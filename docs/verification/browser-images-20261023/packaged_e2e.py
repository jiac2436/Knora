"""Actual packaged host/worker and HTTP image download, in an empty temporary library."""
import json,os,select,subprocess,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
root=Path(__file__).resolve().parents[3]
record=json.loads((Path(__file__).parent/'runtime-browser-images.json').read_text())
runtime=root/record['runtime']
credential='synthetic-browser-image-test-'+'a'*40
with tempfile.TemporaryDirectory(prefix='know-browser-images-') as temporary:
    home=Path(temporary)
    environment={key:value for key,value in os.environ.items() if not key.startswith('PKA_')}
    environment.update(PKA_ENVIRONMENT='local',PKA_DATA_DIR=str(home/'data'),PKA_CACHE_DIR=str(home/'cache'),
        PKA_LOG_DIR=str(home/'logs'),PKA_SEMANTIC_ENABLED='0',PKA_WEB_ALLOW_FAKE_IP='1',PKA_WORKER_ROOT=str(runtime/'backend'))
    with (home/'host.log').open('wb') as log:
        process=subprocess.Popen([str(runtime/'python/bin/python3'),'-I','-B',str(runtime/'backend/app/local_host.py')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,env=environment)
        try:
            process.stdin.write((credential+'\n').encode());process.stdin.flush()
            assert select.select([process.stdout],[],[],30)[0]
            ready=json.loads(process.stdout.readline());assert ready['event']=='ready',ready
            address=ready['address']
            def call(path,data=None,method=None,key=None):
                headers={'Authorization':'Bearer '+credential,'Content-Type':'application/json'}
                if key:headers['Idempotency-Key']=key
                request=Request(address+path,headers=headers,data=json.dumps(data).encode() if data is not None else None,method=method)
                with urlopen(request,timeout=10) as response:return json.load(response)
            domain=call('/v1/domains',{'name':'浏览器图片合成测试'})
            payload={'capture_type':'browser_page','title':'浏览器图片合成测试','source_url':'https://www.xiaohongshu.com/explore/synthetic',
                'content':{'text':'这是独立临时知识库的合成文字。','image_urls':['https://raw.githubusercontent.com/github/explore/main/topics/python/python.png']},
                'coverage':'partial_text','context':{'domain_id':domain['id']},'client':{'kind':'browser_extension','version':'0.1.0'}}
            created=call('/v1/captures',payload,key='synthetic-browser-images')
            assert call('/v1/captures',payload,key='synthetic-browser-images')['id']==created['id']
            deadline=time.monotonic()+60
            while True:
                detail=call('/v1/sources/'+created['id'])
                image=detail['images'][0]
                if image['status'] in ('saved','unavailable'):break
                assert time.monotonic()<deadline,'image worker timeout'
                time.sleep(.5)
            assert image['status']=='saved',image['failure_reason']
            assert detail['browser_image_urls']==payload['content']['image_urls']
            assert detail['extracted_text']==payload['content']['text']
            assert 'know-image://'+image['id'] in detail['reading_text']
            with urlopen(Request(address+image['download_path'],headers={'Authorization':'Bearer '+credential}),timeout=10) as response:
                body=response.read();assert body.startswith(b'\x89PNG')
            assert len(body)==image['size_bytes']
            print('PASS: packaged host, worker, real public PNG download, local binary read, reading placeholders, image-link readback and idempotency; temporary library only')
        finally:
            process.stdin.close()
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
