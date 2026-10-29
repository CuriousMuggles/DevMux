"""真实Edge观察空闲后输入到下一次读取的延迟，以及慢读取时的并发上限。"""
import json
from tests.test_ui_e2e import _cdp_eval


def check(endpoint):
    result=_cdp_eval(endpoint,'',r"""(async()=>{
      const delay=ms=>new Promise(r=>setTimeout(r,ms));
      const wait=async predicate=>{for(let i=0;i<200;i++){if(predicate())return;await delay(10);}throw Error('串口输入唤醒等待超时');};
      const view=[...SerialIntegration.windows.values()][0],panel=view.panel;
      const format=panel.querySelector('.serial-format');format.value='text';format.dispatchEvent(new Event('change'));
      const command=panel.querySelector('.terminal-cmd-textarea'),send=panel.querySelector('.serial-send');
      const original=callWS;
      let active=0,maximum=0,hold=0,arm='',started=0,nextRead=0;
      const text=()=>{const b=view.term.buffer.active;return Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString(true)||'').join('|');};
      callWS=(type,payload,...rest)=>{
        if(type!=='extension-tool'||payload.tool!=='serial'||payload.action!=='read'||payload.arguments.sessionId!==view.sessionId)return original(type,payload,...rest);
        if(started&&!nextRead)nextRead=performance.now();
        active++;maximum=Math.max(maximum,active);
        return original(type,payload,...rest).then(async result=>{
          if(hold)await delay(hold);
          active--;
          if(arm){const value=arm;arm='';setTimeout(()=>{started=performance.now();command.value=value;send.click();},2);}
          return result;
        },error=>{active--;throw error;});
      };
      try{
        const samples=[];
        for(let i=0;i<3;i++){
          await delay(1200);started=0;nextRead=0;arm='WAKE_'+i;
          await wait(()=>started&&nextRead);samples.push(nextRead-started);
          await wait(()=>text().includes('WAKE_'+i));
        }
        const median=[...samples].sort((a,b)=>a-b)[1];
        if(median>=30)throw Error('输入仍等待空闲读轮询：'+JSON.stringify(samples));
        hold=80;await wait(()=>active===1);
        for(let i=0;i<6;i++){command.value='BURST_'+i;send.click();}
        await wait(()=>text().includes('BURST_5'));
        if(maximum!==1)throw Error('输入唤醒产生并行读取：'+maximum);
        return {idleInputToReadMs:samples.map(n=>Math.round(n*100)/100),medianMs:Math.round(median*100)/100,maxConcurrentReads:maximum};
      }finally{callWS=original;}
    })()""")
    print('  [PASS] 实际Edge空闲输入唤醒/慢读取不堆积：'+json.dumps(result,ensure_ascii=False),flush=True)
    return result
