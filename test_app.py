import http.client,json,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
import app

class ImportTests(unittest.TestCase):
 def test_urls_ranges(self):
  self.assertEqual(app.canonical_url('https://youtu.be/iF9di-AySFo?si=abc'),'https://www.youtube.com/watch?v=iF9di-AySFo')
  for url in ['http://youtu.be/iF9di-AySFo','https://youtube.com.evil.org/watch?v=iF9di-AySFo','https://user@youtube.com/watch?v=iF9di-AySFo','https://127.0.0.1/watch?v=iF9di-AySFo','https://www.youtube.com/playlist?list=abc']:
   with self.assertRaises(app.UserError):app.canonical_url(url)
  for start,end in [(0,301),(-1,10),(10,9),(0,float('nan')),(True,20),(1700,1900)]:
   with self.assertRaises(app.UserError):app.clip_range(start,end)
  self.assertEqual(app.clip_range(10,70),(10,70))
 def test_http_lifecycle(self):
  server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
  threading.Thread(target=server.serve_forever,daemon=True).start()
  def request(method,path,body=None,auth=True,origin=app.ORIGIN):
   c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=4);headers={'Origin':origin}
   if auth:headers['Authorization']='Bearer test-secret-at-least-24-characters'
   if body is not None:headers['Content-Type']='application/json';body=json.dumps(body)
   c.request(method,path,body,headers);r=c.getresponse();data=r.read();status=r.status;c.close();return status,data
  def fake_command(command,job,timeout=120):
   if '--dump-single-json' in command:return json.dumps({'title':'테스트 영상','duration':236,'availability':'public'})
   if command[0]=='ffprobe':return json.dumps({'format':{'duration':'10'},'streams':[{'codec_type':'video'}]})
   (Path(job['dir'])/'clip.mp4').write_bytes(b'fixture'*100);return ''
  def finish(ident):
   deadline=time.monotonic()+3
   while app.jobs[ident]['status'] in ('queued','working') and time.monotonic()<deadline:time.sleep(.01)
   app.worker.submit(lambda: None).result(timeout=2)
  try:
   with patch.object(app,'TOKEN','test-secret-at-least-24-characters'),patch.object(app,'run_command',side_effect=fake_command):
    self.assertEqual(request('GET','/v1/status',auth=False)[0],401)
    self.assertEqual(request('GET','/v1/status',origin='https://evil.example')[0],403)
    self.assertEqual(request('GET','/v1/status')[0],200)
    self.assertEqual(request('POST','/v1/jobs',{'kind':'import','url':'https://evil.example','start':0,'end':10})[0],400)
    status,body=request('POST','/v1/jobs',{'kind':'import','url':'https://youtu.be/iF9di-AySFo','start':0,'end':10})
    self.assertEqual(status,202);ident=json.loads(body)['id'];path='/v1/jobs/'+ident;finish(ident)
    self.assertEqual(json.loads(request('GET',path)[1])['status'],'ready')
    self.assertEqual(request('GET',path+'/file')[1],b'fixture'*100)
    directory=app.jobs[ident]['dir'];self.assertEqual(request('DELETE',path)[0],200)
    self.assertFalse(Path(directory).exists());self.assertEqual(request('GET',path)[0],404)
    status,body=request('POST','/v1/jobs',{'kind':'import','url':'https://youtu.be/iF9di-AySFo','start':230,'end':240})
    ident=json.loads(body)['id'];finish(ident)
    self.assertEqual(app.jobs[ident]['status'],'failed');self.assertFalse(Path(app.jobs[ident]['dir']).exists())
  finally:server.shutdown();server.server_close()
 def test_cancel_cleanup(self):
  directory=tempfile.mkdtemp(dir=app.ROOT);cancel=threading.Event();cancel.set()
  job={'cancel':cancel,'status':'queued','kind':'import','dir':directory}
  app.process_job(job);self.assertEqual(job['status'],'cancelled');self.assertFalse(Path(directory).exists())

if __name__=='__main__':unittest.main()
