"""实际Edge：未知发送结果仍可接收，迟到回执恢复新输入且不重放。"""
from tests.test_ui_e2e import _cdp_eval


def check(endpoint):
    result=_cdp_eval(endpoint,'',r"""(async()=>{
      const delay=ms=>new Promise(r=>setTimeout(r,ms));
      const wait=async fn=>{for(let n=0;n<200;n++){if(fn())return;await delay(20);}throw Error('回执UI等待超时');};
      const window1=[...SerialIntegration.windows.values()][0], panel=window1.panel;
      const command=panel.querySelector('.terminal-cmd-textarea');
      const format=panel.querySelector('.serial-format');format.value='text';format.dispatchEvent(new Event('change'));
      const text=()=>{const b=window1.term.buffer.active;return Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString(true)||'').join('|');};
      const original=callWS;let pending=false,deadline=0,intercepted=false,writeCount=0;
      callWS=async(type,args,...rest)=>{
        const result=await original(type,args,...rest);
        if(type==='extension-tool'&&args.tool==='serial'&&args.arguments?.sessionId===window1.sessionId){
          if(args.action==='write'){
            writeCount++;
            if(!intercepted){intercepted=true;pending=true;deadline=Date.now()+800;return {...result,success:false,operationUncertain:true,output:'模拟丢失回执'};}
          }
          if(args.action==='read'&&intercepted&&result.success){
            pending=Date.now()<deadline;
            result.data={...result.data,serialStreamProtocol:1,inputUncertain:pending};
          }
        }
        return result;
      };
      try {
        command.value='ACK_TEST';panel.querySelector('.serial-send').click();
        command.value='PREQUEUED';panel.querySelector('.serial-send').click();
        await wait(()=>command.disabled&&panel.querySelector('.serial-status').textContent.includes('继续接收'));
        // 待确认期间的尝试必须被丢弃，不能在回执恢复后补发。
        command.value='OLD_QUEUED';panel.querySelector('.serial-send').onclick();
        await wait(()=>text().includes('ACK_TEST'));
        await wait(()=>!command.disabled);
        command.value='AFTER_ACK';panel.querySelector('.serial-send').click();
        await wait(()=>text().includes('AFTER_ACK'));
        await delay(100);
        if(writeCount!==2)throw Error('未知结果后重发或补发了旧输入');
        return {ok:true};
      } finally {callWS=original;}
    })()""")
    assert result['ok'],result
    print('  [PASS] 实际Edge未知结果继续接收/输入暂停；明确迟到回执恢复新输入，旧输入未重发')
