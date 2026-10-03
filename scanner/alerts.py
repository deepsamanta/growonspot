"""Independent scanner alerts using the existing Telegram bot credentials."""
import json
import queue
import threading
import time
import requests

TITLES={'ENTRY_FILLED':'Entry executed','ORDER_SUBMITTED':'Market order submitted',
        'ORDER_REJECTED':'Entry rejected','ORDER_UNCERTAIN':'Entry outcome requires review',
        'EXIT_CONFIRMED':'Exit confirmed','TP_EXIT_REQUESTED':'Take-profit exit requested',
        'EXIT_UNCERTAIN':'Exit outcome requires review','TP_PENDING':'Take-profit attachment pending',
        'OWNERSHIP_CONFLICT':'Position needs manual review','SCANNER_STARTED':'Scanner monitoring started',
        'SCANNER_ERROR':'Scanner error','RECONCILE_ERROR':'Position reconciliation error'}


class Alerts:
    def __init__(self,token,chat_id):
        self.token,self.chat_id=token,chat_id
        self.pending=queue.Queue(maxsize=100)
        self.recent={};self.lock=threading.Lock();self.stop=threading.Event()
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def emit(self,event,**fields):
        now=time.time();key=(event,fields.get('pair'),fields.get('trade_id'),fields.get('reason'))
        with self.lock:
            self.recent={k:v for k,v in self.recent.items() if now-v<300}
            if key in self.recent:return
            self.recent[key]=now
        print(json.dumps({'event':event,'time':now,**fields}),flush=True)
        if event not in TITLES:return
        text='[MARKET SCANNER] '+TITLES[event]+'\n'+'\n'.join(f'{k}: {str(v)[:200]}' for k,v in fields.items())
        try:self.pending.put_nowait(text[:4000])
        except queue.Full:print(json.dumps({'event':'SCANNER_ALERT_QUEUE_FULL'}),flush=True)

    def run(self):
        session=requests.Session()
        while not self.stop.is_set() or not self.pending.empty():
            try:text=self.pending.get(timeout=.2)
            except queue.Empty:continue
            delivered=False
            for n in range(3):
                try:
                    r=session.post('https://api.telegram.org/bot'+self.token+'/sendMessage',
                        json={'chat_id':self.chat_id,'text':text},timeout=(3,5))
                    body=r.json()
                    if r.status_code==200 and body.get('ok') is True:delivered=True;break
                    if r.status_code!=429 and r.status_code<500:break
                except (requests.RequestException,ValueError):pass
                if self.stop.wait(2**n):break
            if not delivered:print(json.dumps({'event':'SCANNER_ALERT_DELIVERY_FAILED'}),flush=True)
            self.pending.task_done()
        session.close()

    def close(self):
        self.stop.set();self.thread.join(timeout=10)
