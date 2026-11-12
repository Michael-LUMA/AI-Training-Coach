"""M6 route and builder acceptance, fully offline including real loopback HTTP."""
import base64
from contextlib import closing
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import auth
import coach_server as server
import db
import dialog_store as dialog
import memory_service as memory
import ride_chat
import ride_general
import ride_knowledge
import ride_live
from action_cards import ActionError
from tests.test_dialog_store import SqliteDatabase
from tests.test_ride_chat import transport, facts_of
from tests.test_ride_live import live_facts


class DialogRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.database=SqliteDatabase(Path(self.temp.name)/"dialog.sqlite3")
        self.database.initialize()
        for boundary in self.database.patches():
            boundary.start(); self.addCleanup(boundary.stop)
        self.app=server.App(store=SimpleNamespace(path=Path(self.temp.name)/"coach.sqlite3"))
        def identity(token):
            if token not in ("token-a","token-b"): raise auth.InvalidToken("synthetic bad token")
            return token[-1]
        for boundary in (patch.object(auth,"whoami",side_effect=identity),
                         patch.object(memory,"_records",return_value=[]),
                         patch.dict(os.environ,{"AGENT_PLANNER":"off","DASHSCOPE_API_KEY":""})):
            boundary.start(); self.addCleanup(boundary.stop)
        self.run=patch.object(server.llm_router,"run",side_effect=self.answer)
        self.router=self.run.start(); self.addCleanup(self.run.stop)

    def answer(self,lane,builder,**unused):
        return {"llm_status":"ok","draft":{"speech":"可以，我会记住这次对话。"},
                "fallback":None,"usage":None}

    def request(self,path,payload,*,browser=True):
        body=json.dumps(payload).encode()
        headers={"Host":"127.0.0.1:8794","Origin":"http://127.0.0.1:8794",
                 "Content-Type":"application/json","Content-Length":str(len(body))}
        if browser: headers["X-Coach-Token"]=self.app.token
        return self.app.dispatch("POST",path,headers,body)

    def test_chat_stores_pair_and_next_cloud_request_receives_same_user_five_rounds(self):
        for i in range(5):
            dialog.save_exchange("a",f"问题{i}",f"回答{i}")
        dialog.save_exchange("b","属于乙的问题","属于乙的回答")
        self.app._convs["same"]=[[{"q":"共享本机秘密","a":"不能泄露"}],time.monotonic()]
        def run(lane,builder,**unused):
            return builder(api_key="synthetic")
        self.app.llm_post=transport("可以继续。")
        with patch.object(server.llm_router,"run",side_effect=run), patch.object(server,"build_general_answer",wraps=server.build_general_answer) as build:
            # Force deterministic route selection to general after no card match.
            result=self.app._chat_ride({"token":"token-a","question":"继续","conv_id":"same"})
        self.assertEqual(result["speech_status"],"ok")
        history=build.call_args.kwargs["history"]
        self.assertEqual(history,[{"q":f"问题{i}","a":f"回答{i}"} for i in range(5)])
        self.assertNotIn("属于乙",json.dumps(history,ensure_ascii=False))
        listing=dialog.list_turns("a",export=True)
        self.assertEqual(listing["total_rounds"],6)
        self.assertEqual(listing["turns"][-2]["content"],"继续")
        self.assertEqual(self.app._convs["same"][0][0]["q"],"共享本机秘密")

    def test_cloud_weather_deterministic_exit_persists_without_model(self):
        value=self.app._chat_ride({"token":"token-a","question":"今天天气怎样"})
        self.assertEqual(value["speech_status"],"ok")
        self.router.assert_not_called()
        self.assertEqual(dialog.list_turns("a")["total_messages"],2)

    def test_cloud_missing_live_snapshot_persists_honest_template(self):
        value=self.app._chat_ride({"token":"token-a","question":"我现在心率多少","live_id":"live"})
        self.assertEqual(value["lane"],"live")
        self.assertEqual(value["speech"],server.LIVE_TEMPLATE)
        self.assertEqual(dialog.get_recent("a")[-1]["a"],value["speech"])
        self.router.assert_not_called()

    def test_cloud_live_answer_exit_persists_pair(self):
        self.app._live[("a","live")]=[SimpleNamespace(facts=live_facts),0,time.monotonic(),{}]
        with patch.object(server.llm_router,"run",return_value={"llm_status":"ok","speech":"现在还看不到心率。","usage":None,"fallback":None}):
            result=self.app._chat_ride({"token":"token-a","question":"我现在心率多少","live_id":"live"})
        self.assertEqual(result["lane"],"live")
        self.assertEqual(dialog.list_turns("a")["total_messages"],2)

    def test_general_480_character_reply_remains_valid_and_storable(self):
        speech="可以。"*160
        self.assertEqual(len(speech),480)
        with patch.object(server.llm_router,"run",return_value={"llm_status":"ok","draft":{"speech":speech},"usage":None,"fallback":None}):
            result=self.app._chat_ride({"token":"token-a","question":"讲得详细些"})
        self.assertEqual(result["speech"],speech)
        self.assertEqual(dialog.get_recent("a")[-1]["a"],speech)
        self.assertEqual(ride_chat._check_history([{"q":"问题","a":speech}])[0]["a"],speech)

    def test_failure_does_not_store_question_or_answer(self):
        for status in ("upstream_error","rejected_bad_schema","rejected_unverifiable_number"):
            with patch.object(server.llm_router,"run",return_value={"llm_status":status,"draft":None,"fallback":None,"usage":None}):
                self.app._chat_ride({"token":"token-a","question":"我骑了多久"})
        self.assertEqual(dialog.list_turns("a")["total_messages"],0)

    def test_storage_failure_is_explicit_503_and_does_not_fallback(self):
        with patch.object(dialog,"get_recent",side_effect=db.DatabaseOperationError("secret SQL")):
            with self.assertRaises(ActionError) as caught:
                self.app._chat_ride({"token":"token-a","question":"继续"})
        self.assertEqual(caught.exception.status,503)
        self.assertNotIn("secret",str(caught.exception))
        self.router.assert_not_called()
        with patch.object(dialog,"save_exchange",side_effect=db.DatabaseOperationError("secret SQL")):
            with self.assertRaises(ActionError) as caught:
                self.app._chat_ride({"token":"token-a","question":"继续"})
        self.assertEqual(caught.exception.status,503)
        self.assertEqual(dialog.list_turns("a")["total_messages"],0)

    def test_capacity_409_before_model_export_then_explicit_cleanup(self):
        with patch.object(dialog,"MAX_ROUNDS",1):
            dialog.save_exchange("a","旧问题","旧回答")
            with self.assertRaises(ActionError) as caught:
                self.app._chat_ride({"token":"token-a","question":"继续"})
            self.assertEqual(caught.exception.status,409)
            self.assertEqual(caught.exception.code,"dialog_export_required")
            self.router.assert_not_called()
            exported=self.request("/api/dialog/list",{"token":"token-a","export":True})
            self.assertEqual(exported["total_messages"],2)
            self.assertTrue(exported["export_required"])
            self.app._chat_ride({"token":"token-a","question":"继续","dialog_prune_confirmed":True})
            self.assertEqual(dialog.list_turns("a")["total_messages"],2)
            self.assertNotIn("旧问题",json.dumps(dialog.list_turns("a"),ensure_ascii=False))

    def test_list_and_delete_routes_derive_identity_and_hard_delete(self):
        pair=dialog.save_exchange("a","问题","回答")
        dialog.save_exchange("b","乙问题","乙回答")
        listed=self.request("/api/dialog/list",{"token":"token-a","limit":1})
        self.assertEqual(len(listed["turns"]),1)
        self.assertTrue(listed["has_more"])
        identifier=pair["turns"][0]["id"]
        self.assertEqual(self.request("/api/dialog/delete",{"token":"token-b","id":identifier}),{"deleted":0})
        self.assertEqual(self.request("/api/dialog/delete",{"token":"token-a","id":identifier}),{"deleted":1})
        self.assertEqual(self.request("/api/dialog/delete",{"token":"token-a","all":True}),{"deleted":1})
        self.assertEqual(self.request("/api/dialog/list",{"token":"token-a","export":True})["turns"],[])
        self.assertEqual(dialog.list_turns("b")["total_messages"],2)

    def test_cloud_token_and_browser_gate_are_distinct(self):
        for path in ("/api/dialog/list","/api/dialog/delete"):
            with self.assertRaises(ActionError) as caught:
                self.request(path,{"token":"token-a"},browser=False)
            self.assertEqual(caught.exception.status,403)
        for payload,status in (({},401),({"token":"invalid"},401),({"token":"token-a","user_id":"b"},400),
                               ({"token":"token-a","export":1},400),({"token":"token-a","limit":True},400)):
            with self.subTest(payload=payload),self.assertRaises(ActionError) as caught:
                self.request("/api/dialog/list",payload)
            self.assertEqual(caught.exception.status,status)
        for payload in ({"token":"token-a","all":False},{"token":"token-a","all":1},
                        {"token":"token-a","all":True,"id":"a"}):
            with self.assertRaises(ActionError): self.request("/api/dialog/delete",payload)
        for payload in ({"question":"继续","dialog_prune_confirmed":True},
                        {"token":"token-a","question":"继续","dialog_prune_confirmed":"true"}):
            with self.assertRaises(ActionError): self.app._chat_ride(payload)

    def test_anonymous_stays_three_round_ttl_and_never_uses_cloud(self):
        self.app.ride_dir.mkdir(parents=True)
        with patch.object(dialog,"get_recent",side_effect=AssertionError("cloud read")), patch.object(dialog,"save_exchange",side_effect=AssertionError("cloud write")):
            for i in range(5): self.app._chat_ride({"question":f"你好{i}","conv_id":"anon"})
        self.assertEqual(len(self.app._dialog_read("anon")),3)

    def test_loopback_http_retains_private_browser_origin_gates_and_no_store(self):
        password="synthetic-M6-private-password-12345678"
        basic="Basic "+base64.b64encode(("boss:"+password).encode()).decode()
        with patch.dict(os.environ,{"LUMA_COACH_TEST_PASSWORD":password}),patch.object(server,"_bind_config",return_value=("127.0.0.1",0)):
            app=server.App(server.CoachStore(Path(self.temp.name)/"http.sqlite3"))
            httpd=server.create_server(app=app)
            worker=Thread(target=httpd.serve_forever,kwargs={"poll_interval":0.02},daemon=True); worker.start()
            try:
                def request(path,body,*,private=True,browser=True,origin=True):
                    headers={"Origin":"http://127.0.0.1:"+str(httpd.server_port),"Content-Type":"application/json"}
                    if not origin: headers["Origin"]="https://evil.invalid"
                    if private: headers["Authorization"]=basic
                    if browser: headers["X-Coach-Token"]=app.token
                    with closing(http.client.HTTPConnection("127.0.0.1",httpd.server_port,timeout=5)) as client:
                        client.request("POST",path,json.dumps(body).encode(),headers)
                        response=client.getresponse()
                        return response.status,dict(response.getheaders()),json.loads(response.read())
                body={"token":"token-a","export":True}
                self.assertEqual(request("/api/dialog/list",body,private=False)[0],401)
                self.assertEqual(request("/api/dialog/list",body,browser=False)[0],403)
                self.assertEqual(request("/api/dialog/list",body,origin=False)[0],403)
                self.assertEqual(request("/api/dialog/list",{"token":"bad"})[0],401)
                dialog.save_exchange("a","HTTP问题","HTTP回答")
                status,headers,value=request("/api/dialog/list",body)
                self.assertEqual(status,200)
                self.assertEqual(headers["Cache-Control"],"no-store")
                self.assertEqual(value["total_messages"],2)
                self.assertNotIn("token-a",json.dumps(value))
                status,_,value=request("/api/dialog/delete",{"token":"token-a","all":True})
                self.assertEqual((status,value),(200,{"deleted":2}))
            finally:
                httpd.shutdown(); httpd.server_close(); worker.join(timeout=5)
            self.assertFalse(worker.is_alive())


class FiveRoundBuilderTests(unittest.TestCase):
    def test_five_rounds_are_user_facts_never_system_prompt(self):
        turns=[{"q":f"问题{i}","a":"回答"} for i in range(5)]
        app=server.App(store=SimpleNamespace(path=Path(tempfile.gettempdir())/"unused-M6.sqlite3"))
        calls=[lambda post: ride_chat.build_ride_chat("继续",history=turns,api_key="synthetic",http_post=post),
               lambda post: ride_general.build_general_answer("继续",history=turns,api_key="synthetic",http_post=post),
               lambda post: ride_knowledge.build_knowledge_answer("骑行时怎样补水",app.knowledge_cards,history=turns,api_key="synthetic",http_post=post),
               lambda post: ride_live.build_live_answer("现在骑得如何",live_facts(),history=turns,api_key="synthetic",http_post=post)]
        for call in calls:
            post=transport("可以继续。")
            call(post)
            self.assertTrue(post.called)
            body=json.loads(post.call_args.args[0].data)
            self.assertNotIn("问题0",body["messages"][0]["content"])
            facts=json.loads(body["messages"][1]["content"])
            self.assertEqual(facts["recent_dialog"],turns)

    def test_history_numbers_do_not_become_verified_current_facts(self):
        turns=[{"q":"问","a":"上次是999 bpm。"} for _ in range(5)]
        result=ride_chat.build_ride_chat("当前心率",history=turns,api_key="synthetic",http_post=transport("现在心率999 bpm。"))
        self.assertEqual(result["llm_status"],"rejected_unverifiable_number")


if __name__=="__main__": unittest.main()
