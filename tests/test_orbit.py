import json
import tempfile
import unittest
from pathlib import Path
import httpx
from fastapi.testclient import TestClient

from orbit.config import Settings
from orbit.storage import Storage
from orbit.harness import Harness
from orbit.app import create_app
from orbit.tools import checked_path, execute_tool


def client_for(responses):
    records = []
    def handler(request):
        records.append(json.loads(request.content))
        if not responses:
            raise AssertionError("Unexpected model request")
        return httpx.Response(200, json={"choices": [{"message": responses.pop(0)}]})
    return httpx.Client(transport=httpx.MockTransport(handler)), records


def tool_call(name, arguments, call_id="t1"):
    return {"id":call_id,"type":"function","function":{"name":name,"arguments":json.dumps(arguments)}}


class OrbitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        self.settings = Settings(model="mock-local",base_url="http://test.invalid/v1",api_key="",workspace=self.workspace,database=base / "db.sqlite3",bind="127.0.0.1",port=8765)

    def make_engine(self, replies):
        client, records = client_for(replies)
        self.addCleanup(client.close)
        return Harness(self.settings, Storage(self.settings.database), client), records

    def test_basic_chat_persisted(self):
        engine, records = self.make_engine([{"role":"assistant","content":"Hello from ORBIT."}])
        session=engine.start("ALEX","First task")
        result=engine.chat(session["id"],"hi")
        self.assertEqual(result["status"],"completed")
        self.assertEqual(result["assistant"],"Hello from ORBIT.")
        self.assertEqual(records[0]["model"],"mock-local")
        reopened=Storage(self.settings.database)
        self.assertEqual(len(reopened.messages(session["id"])),2)
        self.assertEqual(reopened.messages(session["id"])[0]["role"],"user")

    def test_readonly_tool_loop(self):
        (self.workspace / "notes.txt").write_text("hello secure world")
        engine, records=self.make_engine([
            {"role":"assistant","content":None,"tool_calls":[tool_call("read_file",{"path":"notes.txt"})]},
            {"role":"assistant","content":"File says hello."},
        ])
        session=engine.start("HERMES","Read")
        result=engine.chat(session["id"],"read notes")
        self.assertEqual(result["status"],"completed")
        self.assertEqual(result["assistant"],"File says hello.")
        self.assertEqual(records[1]["messages"][-1]["role"],"tool")
        self.assertIn("hello secure world",records[1]["messages"][-1]["content"])
        self.assertEqual(result["audit"][0]["decision"],"auto-read")

    def test_write_requires_approval(self):
        engine, records=self.make_engine([
            {"role":"assistant","content":None,"tool_calls":[tool_call("write_file",{"path":"new.txt","content":"new code"})]},
            {"role":"assistant","content":"Created file."},
        ])
        session=engine.start("ALEX","Write")
        paused=engine.chat(session["id"],"make file")
        self.assertEqual(paused["status"],"approval_required")
        self.assertFalse((self.workspace / "new.txt").exists())
        self.assertEqual(len(records),1)
        result=engine.approve(paused["approvals"][0]["id"],True)
        self.assertEqual(result["status"],"completed")
        self.assertEqual((self.workspace / "new.txt").read_text(),"new code")
        self.assertEqual(result["audit"][0]["decision"],"approved")

    def test_reject_write_and_continue(self):
        engine, records=self.make_engine([
            {"role":"assistant","content":None,"tool_calls":[tool_call("write_file",{"path":"danger.txt","content":"bad"})]},
            {"role":"assistant","content":"Cancelled."},
        ])
        session=engine.start("NOVA","Denied")
        paused=engine.chat(session["id"],"write")
        result=engine.approve(paused["approvals"][0]["id"],False)
        self.assertFalse((self.workspace/"danger.txt").exists())
        self.assertEqual(result["assistant"],"Cancelled.")
        self.assertEqual(result["audit"][0]["decision"],"denied")

    def test_escape_and_secret_block(self):
        (self.workspace / ".env").write_text("SECRET=123")
        for path in ("../outside", "/etc/passwd", ".env", ".env.production", "a/private.key"):
            with self.assertRaises((PermissionError, ValueError)):
                checked_path(self.workspace,path)
        with self.assertRaises(PermissionError):
            execute_tool("write_file",{"path":"test.txt","content":"x"},self.workspace)
        outside=Path(self.temp.name)/"outside.txt"
        outside.write_text("restricted")
        (self.workspace / "alias").symlink_to(outside)
        with self.assertRaises(PermissionError):
            checked_path(self.workspace,"alias")
        (self.workspace / "secret_alias").symlink_to(self.workspace / ".env")
        with self.assertRaises(PermissionError):
            checked_path(self.workspace,"secret_alias")

    def test_api_and_origin_protection(self):
        model, _ = client_for([{"role":"assistant","content":"pong"}])
        self.addCleanup(model.close)
        app=create_app(self.settings,model)
        with TestClient(app) as client:
            health=client.get('/api/health').json()
            self.assertEqual(health["write_policy"],"human_approval")
            denied=client.post('/api/sessions',json={"profile":"ALEX"},headers={"Origin":"https://bad.example"})
            self.assertEqual(denied.status_code,403)
            session=client.post('/api/sessions',json={"profile":"ALEX"})
            self.assertEqual(session.status_code,201)
            sid=session.json()["id"]
            reply=client.post('/api/sessions/'+sid+'/chat',json={"content":"ping"})
            self.assertEqual(reply.json()["assistant"],"pong")
            self.assertEqual(client.get('/api/sessions/'+sid).status_code,200)
            self.assertIn('text/html',client.get('/').headers['content-type'])


if __name__ == '__main__':
    unittest.main()
