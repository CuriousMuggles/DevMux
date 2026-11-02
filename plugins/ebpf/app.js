(() => {
  const {ui,call,hostCall,listen}=DevMuxPlugin;
  const page=ui.mount({title:'Linux eBPF 调试',description:'有限时长采集 · 系统调用延迟分布与调用栈'});
  page.element.classList.add('ebpf-page');
  const toolbar=ui.element('div','ebpf-toolbar'),devices=ui.element('select');devices.setAttribute('aria-label','目标设备');
  const form=ui.form([{name:'pid',label:'进程 PID',type:'number',min:1,max:4194304,required:true},{name:'duration',label:'采集时长（秒）',type:'number',min:1,max:30,default:5,required:true},{name:'mode',label:'采集内容',type:'select',options:[{value:'session',label:'一键诊断：延迟分布 + 慢调用双栈'},{value:'latency',label:'系统调用耗时分布'},{value:'kernel',label:'最近系统调用内核栈'},{value:'user',label:'最近系统调用用户栈'}]}]);
  const summary=ui.element('div','ebpf-summary','等待采集'),view=ui.element('div'),raw=ui.log({limit:200});
  const shell=ui.element('div','ebpf-shell'),sidebar=ui.element('aside','ebpf-sidebar'),workspace=ui.element('section','ebpf-workspace'),panels=ui.element('div','ebpf-panels');
  const histogram=ui.element('section','ebpf-panel'),stack=ui.element('section','ebpf-panel'),stackBody=ui.element('div','ebpf-stack-body'),history=ui.element('div','ebpf-history');
  const records=[];let chosen=null,stackMode='kernel';
  const element=ui.element;
  const analysisPanel=element('section','ebpf-analysis ebpf-panel');
  const correlatedPanel=element('section','ebpf-analysis ebpf-panel');
  const importFile=element('input');importFile.type='file';importFile.accept='.json,application/json';importFile.hidden=true;
  const correlatedBody=element('div'),correlatedStacks=element('div','ebpf-correlated-stacks');let importedSession=null;
  const importButton=ui.button('导入联合采集报告',()=>importFile.click());
  correlatedPanel.append(element('h2','','慢调用与关联栈'),element('p','ebpf-note','联合采集将耗时与入口栈按同一 TID 关联；当前只保留最近一条慢调用，其余计入 droppedSlowCalls，不代表完整慢调用列表。'),importButton,importFile,correlatedBody,correlatedStacks);
  function renderSession(report,source='实时联合采集'){
    correlatedBody.replaceChildren();correlatedStacks.replaceChildren();
    correlatedBody.append(element('p','ebpf-note',`${source} · 会话 ${report.sessionId} · PID ${report.pid} · 阈值 ${(report.slowThresholdNs/1e6).toFixed(3)} ms`));
    if(report.symbolStats)correlatedBody.append(element('p','ebpf-note',`内核符号：${report.symbolStats.kernelResolvedFrames||0}/${report.symbolStats.kernelTotalFrames||0} 帧 · 用户符号：${report.symbolStats.resolvedFrames}/${report.symbolStats.totalFrames} 帧 · ${(report.symbolStats.elapsedMs/1000).toFixed(2)} 秒 · 设备端解析`));
    for(const warning of report.symbolWarnings||[])correlatedBody.append(element('p','ebpf-note','符号解析：'+warning));
    correlatedBody.append(ui.table([{key:'metric',label:'采集质量'},{key:'value',label:'值'}],Object.entries(report.quality).map(([metric,value])=>({metric,value}))));
    if(!report.slowCalls.length)correlatedBody.append(element('p','','没有保存的慢调用'));
    for(const sample of report.slowCalls)correlatedBody.append(ui.button(`TID ${sample.tid} · syscall ${sample.syscallId} · ${(sample.durationNs/1e6).toFixed(3)} ms`,()=>showCorrelatedStack(sample)));
    if(report.slowCalls.length)showCorrelatedStack(report.slowCalls[0]);
  }
  const symbolReasons={'mapping-unavailable':'地址不在可执行映射中','file-identity-unavailable':'无法读取与映射设备号及 inode 匹配的文件','file-too-large':'ELF 超过设备端 128 MiB 上限','file-read-failed':'设备无法读取 ELF','unsupported-elf':'不支持的 ELF 格式','invalid-elf':'ELF 结构无效','mapping-offset-mismatch':'文件加载偏移不匹配','no-function-symbol':'此地址没有函数符号（可能已剥离或函数无尺寸信息）','no-symbol-table':'文件没有函数符号表','identity-changed':'解析期间文件或映射发生变化','kernel-scan-incomplete':'内核符号读取达到 4 秒上限，未找到可信边界的地址已保留','kernel-symbol-unavailable':'无法读取内核符号地址（kallsyms 权限或地址被隐藏）','kernel-address-unmapped':'此内核地址没有可信符号边界（可能是 BPF JIT 或未导出符号）'};
  function symbolIdentity(snapshot,addresses){
    const lines=snapshot.trim().split('\n'),identity=lines.pop();
    if(!/^\d+$/.test(identity))throw Error('未取得进程起始标识');
    return JSON.stringify([identity,addresses.map(address=>lines.find(line=>{const range=line.match(/^([0-9a-f]+)-([0-9a-f]+)\s/);return range&&BigInt(address)>=BigInt('0x'+range[1])&&BigInt(address)<BigInt('0x'+range[2]);})||null)]);
  }
  async function autoSymbols(result,pid,before){
    const started=Date.now(),report=result.session;report.symbolWarnings=[];
    const groups={kernel:[...new Set(report.slowCalls.flatMap(s=>s.kernelStack||[]))],user:[...new Set(report.slowCalls.flatMap(s=>s.userStack||[]))]};
    const stats={method:'device-elf-kallsyms',elapsedMs:0,totalFrames:groups.user.length,resolvedFrames:0,kernelTotalFrames:groups.kernel.length,kernelResolvedFrames:0};
    if(!groups.user.length&&!groups.kernel.length)return;
    const execute=async(command,timeout=10)=>{if(cancelRequested)throw Error('符号解析已取消');const r=await device('exec',{target:activeTarget,argv:['shell',command],timeout});if(!r.success||r.truncated)throw Error(r.error||r.stderr||'设备符号读取失败或输出不完整');return r.stdout;};
    const snapshot=()=>execute(`cat /proc/${pid}/maps; sed 's/.*) //' /proc/${pid}/stat | awk '{print $20}'`);
    let helperPath=null;
    try{
      page.message.update('正在设备端解析内核及用户符号，无需传输符号表…');
      const asset=await call('asset',{mode:'symbols'}),deployed=await device('deploy',{target:activeTarget,...asset});
      if(!/^\/tmp\/devmux-ebpf-[0-9a-f]{24}$/.test(deployed.path||''))throw Error('符号辅助程序部署失败');
      helperPath=deployed.path;
      for(const kind of ['kernel','user']){
        const addresses=groups[kind],label=kind==='kernel'?'内核':'用户';if(!addresses.length)continue;
        try{
          let identity=null;
          if(kind==='user'){
            if(!before)throw Error('未取得采集前进程映射');
            identity=symbolIdentity(before,addresses);
            if(identity!==symbolIdentity(await snapshot(),addresses))throw Error('进程或调用栈对应映射变化，未执行符号解析');
          }
          const request=await call('symbol-plan',{pid,kind,addresses,sha256:asset.sha256});
          if(helperPath!==request.path)throw Error('符号辅助程序部署路径不匹配');
          const text=await execute(request.command,request.timeout),resolved=await call('symbol-result',{pid,kind,addresses,text});
          if(kind==='user'&&identity!==symbolIdentity(await snapshot(),addresses))throw Error('解析期间进程或调用栈对应映射变化，已丢弃符号');
          if(kind==='kernel')for(const frame of resolved.frames){
            const value=BigInt(frame.address),probe=(report.kernelProbeRanges||[]).find(p=>value>=BigInt(p.start)&&value<BigInt(p.start)+BigInt(p.size));
            if(probe){frame.symbol=probe.name+' [采集探针]';frame.offset=Number(value-BigInt(probe.start));frame.status='resolved';frame.source='collector-jit-range';}
          }
          resolved.resolvedFrames=resolved.frames.filter(f=>f.status==='resolved').length;
          const lookup=new Map(resolved.frames.map(f=>[BigInt(f.address).toString(),f]));
          for(const sample of report.slowCalls)sample[kind+'Symbols']=(sample[kind+'Stack']||[]).map(a=>lookup.get(BigInt(a).toString())||null);
          stats[kind==='user'?'resolvedFrames':'kernelResolvedFrames']=resolved.resolvedFrames;
          for(const status of new Set(resolved.frames.filter(f=>f.status!=='resolved').map(f=>f.status)))report.symbolWarnings.push(label+'：'+(symbolReasons[status]||status));
        }catch(error){for(const sample of report.slowCalls)sample[kind+'Symbols']=[];report.symbolWarnings.push(label+'：'+error.message);if(cancelRequested)throw error;}
      }
      stats.elapsedMs=Date.now()-started;report.symbolStats=stats;
    }finally{
      if(helperPath)try{await device('exec',{target:activeTarget,argv:['shell',`rm -f ${helperPath}`],timeout:5});}catch{report.symbolWarnings.push('符号辅助程序临时文件清理未确认');}
    }
  }
  function showCorrelatedStack(sample){
    correlatedStacks.replaceChildren(element('h3','',`TID ${sample.tid} · syscall ${sample.syscallId} · ${(sample.durationNs/1e6).toFixed(3)} ms`));
    for(const [key,label]of [['kernelStack','内核栈'],['userStack','用户栈']]){
      const panel=element('section');panel.append(element('h3','',label));
      if(sample[key]===null)panel.append(element('p','ebpf-note','未采到；不推测缺失函数'));
      else panel.append(ui.table([{key:'index',label:'层级'},{key:'address',label:'地址'},{key:'symbol',label:'符号'}],sample[key].map((address,index)=>{const s=sample[key==='userStack'?'userSymbols':'kernelSymbols']?.[index];return {index,address,symbol:s?.symbol?s.symbol+'+0x'+(s.offset||0).toString(16):symbolReasons[s?.status]||'未解析'};})));
      correlatedStacks.append(panel);
    }
  }
  listen(importFile,'change',async()=>{
    const file=importFile.files[0];if(!file)return;importButton.disabled=true;
    try{
      if(file.size>128*1024)throw Error('报告不得超过 128 KiB');
      const report=await call('parse-session',{report:JSON.parse(await file.text())});
      importedSession=report;correlatedBody.replaceChildren();correlatedStacks.replaceChildren();
      correlatedBody.append(element('p','ebpf-note',`导入报告 · 会话 ${report.sessionId} · PID ${report.pid} · ${report.slowCalls.length} 条慢调用记录 · 阈值 ${(report.slowThresholdNs/1e6).toFixed(3)} ms`));
      const q=report.quality;
      correlatedBody.append(ui.table([{key:'metric',label:'采集质量（报告声明）'},{key:'value',label:'值'}],Object.entries(q).map(([metric,value])=>({metric,value}))));
      if(!report.slowCalls.length)correlatedBody.append(element('p','ebpf-note','没有满足阈值的慢调用记录'));
      for(const [index,sample]of report.slowCalls.entries()){
        const button=ui.button(`#${index+1} · TID ${sample.tid} · syscall ${sample.syscallId} · ${(sample.durationNs/1e6).toFixed(3)} ms`,()=>showCorrelatedStack(sample));button.classList.add('ebpf-record');correlatedBody.append(button);
      }
      if(report.slowCalls.length)showCorrelatedStack(report.slowCalls[0]);
      page.message.update('联合采集报告已校验并导入；这不是一次实时采集','success');
    }catch(error){page.message.update('报告导入失败：'+error.message,'error');}
    finally{importFile.value='';importButton.disabled=false;}
  });
  function showAnalysis(record){
    analysisPanel.replaceChildren(element('h2','','证据与分析'));
    if(!record?.result.analysis){analysisPanel.append(element('p','ebpf-note','采集延迟分布后生成分析；调用栈本身不能证明耗时根因。'));return;}
    const a=record.result.analysis;
    analysisPanel.append(element('p','ebpf-note',`依据：延迟采集 #${record.id} · PID ${record.pid} · ${record.duration} 秒 · ${record.time}`));
    const facts=a.facts||[],total=facts.find(f=>f.metric==='completedCalls'),overflow=facts.find(f=>f.metric==='overflowCalls');
    analysisPanel.append(ui.table([{key:'metric',label:'实测事实'},{key:'value',label:'结果'}],[
      {metric:'完成调用数',value:total?.value??'未知'},
      {metric:'长延迟桶下界',value:overflow?`≥ ${(overflow.lowerBoundNs/1e6).toFixed(6)} ms`:'未知'},
      {metric:'长延迟桶调用数',value:overflow?.value??'未知'},
      {metric:'长延迟桶占比',value:a.overflowRatio==null?'无样本':(100*a.overflowRatio).toFixed(2)+'%'}]));
    analysisPanel.append(element('h3','','采集质量'),element('p','ebpf-note',record.result.session?'联合采集器报告以下质量指标；未报告的指标仍为未知。':'数据完整性尚未证明；以下指标未由当前采集器报告。'));
    analysisPanel.append(ui.table([{key:'name',label:'质量指标'},{key:'value',label:'状态'}],[
      {name:'丢失事件数',value:a.quality?.lostEvents??'未知'},
      {name:'未匹配出口数',value:a.quality?.unmatchedExits??'未知'},
      {name:'栈采集失败数',value:a.quality?.stackFailures??'未知'}]));
    for(const [title,items]of [['解释边界',a.limitations],['下一步排查',a.recommendations]]){analysisPanel.append(element('h3','',title));const list=element('ul','ebpf-note');for(const item of items||[])list.append(element('li','',item));if(!list.children.length)list.append(element('li','','当前没有足够样本形成建议'));analysisPanel.append(list);}
  }
  function blank(node,text){node.replaceChildren(element('p','ebpf-empty',text));}
  const same=r=>r.target===devices.value&&r.pid===Number(form.control('pid').value);
  function showStack(){
    kernel.classList.toggle('active',stackMode==='kernel');user.classList.toggle('active',stackMode==='user');
    if(chosen?.result.session){
      const sample=chosen.result.session.slowCalls[0],frames=sample?.[stackMode+'Stack'],symbols=sample?.[stackMode+'Symbols']||[];
      const count=symbols.filter(s=>s?.symbol).length,badge=stack.querySelector('.ebpf-badge');
      badge.textContent=`${stackMode==='user'?'用户':'内核'}符号 ${count}/${frames?.length||0} 帧`;
      if(!frames?.length){blank(stackBody,'当前没有保存的'+(stackMode==='user'?'用户':'内核')+'栈');return;}
      stackBody.replaceChildren(ui.table([{key:'level',label:'层级'},{key:'address',label:'指令地址'},{key:'symbol',label:'符号'}],frames.map((address,level)=>{const s=symbols[level];return {level,address,symbol:s?.symbol?s.symbol+'+0x'+s.offset.toString(16):symbolReasons[s?.status]||'未解析'};})),element('p','ebpf-note',`同一慢调用 · TID ${sample.tid} · syscall ${sample.syscallId} · ${(sample.durationNs/1e6).toFixed(3)} ms`));
      return;
    }
    const record=chosen&&same(chosen)&&chosen.mode===stackMode?chosen:records.find(r=>same(r)&&r.mode===stackMode);
    const badge=stack.querySelector('.ebpf-badge');if(badge)badge.textContent=record?.result.resolvedFrames?'已解析 '+record.result.resolvedFrames+' 帧':'符号未解析';
    if(!record){blank(stackBody,'采集后显示最近一次调用路径');return;}
    stackBody.replaceChildren(ui.table([{key:'level',label:'层级'},{key:'address',label:'指令地址'},{key:'symbol',label:'符号'}],record.result.frames.map((address,level)=>{const symbol=record.result.symbols?.[level];return {level,address,symbol:symbol?symbol.name+'+0x'+symbol.offset.toString(16):'未解析'};})),element('p','ebpf-note',`#${record.id} · ${record.time} · 已解析 ${record.result.resolvedFrames||0}/${record.result.frames.length} 帧 · 独立采集`));for(const warning of record.result.symbolWarnings||[])stackBody.append(element('p','ebpf-note',warning));
  }
  function showHistogram(record){
    const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg'),counts=record.result.buckets,max=Math.max(1,...counts);
    svg.setAttribute('viewBox','0 0 680 360');svg.setAttribute('role','img');svg.setAttribute('aria-label','系统调用延迟分布');
    function node(tag,attrs,text){const n=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;svg.append(n);return n;}
    for(let i=0;i<=4;i++){const y=290-i*60;node('line',{x1:58,x2:658,y1:y,y2:y,class:'grid'});node('text',{x:48,y:y+4,'text-anchor':'end'},Math.round(max*i/4).toLocaleString());}
    counts.forEach((count,i)=>{const h=240*count/max,bar=node('rect',{x:60+i*18.5,y:290-h,width:14,height:h,rx:2,class:i===31?'overflow':'bar'});const t=document.createElementNS(ns,'title');t.textContent=`${i===31?'≥2.031616':(i*0.065536).toFixed(3)} ms：${count} 次`;bar.append(t);});
    for(const i of [0,8,16,24,31])node('text',{x:67+i*18.5,y:313,'text-anchor':'middle'},i===31?'≥2.032':(i*0.065536).toFixed(3));
    node('text',{x:360,y:344,'text-anchor':'middle'},'系统调用延迟（ms）');node('text',{x:58,y:25},'样本数');view.replaceChildren(svg,element('p','ebpf-note',`#${record.id} · ${record.time} · 每桶 65.536 μs；末桶为溢出区间`));
  }
  function show(record){
    chosen=record;raw.clear();raw.appendLine(record.result.raw);
    const latency=['latency','session'].includes(record.mode)?record:records.find(r=>same(r)&&r.mode==='latency');summary.replaceChildren();
    for(const [label,value]of [['调用总数',latency?latency.result.samples.toLocaleString():'—'],['采集时长',record.duration+' 秒'],['溢出样本',latency?String(latency.result.buckets[31]):'—'],['状态','已完成']]){const tile=element('div','ebpf-stat');tile.append(element('span','',label),element('strong','',value));summary.append(tile);}
    if(latency)showHistogram(latency);else blank(view,'采集延迟分布后显示直方图');showAnalysis(latency);
    if(record.mode==='session')stackMode='user';else if(record.mode==='kernel'||record.mode==='user')stackMode=record.mode;showStack();if(record.result.session)renderSession(record.result.session);
    history.replaceChildren();for(const item of records){const button=ui.button(`#${item.id} ${item.mode==='session'?'联合诊断':item.mode==='latency'?'延迟分布':item.mode==='kernel'?'内核栈':'用户栈'} · 已完成`,()=>{if(busy)return;if(!Array.from(devices.options).some(o=>o.value===item.target)){page.message.update('记录对应的设备不在当前列表中','warning');return;}devices.value=item.target;form.control('pid').value=item.pid;form.control('duration').value=item.duration;form.control('mode').value=item.mode;show(item);page.message.update('正在查看历史记录 #'+item.id);});button.classList.add('ebpf-record');button.classList.toggle('active',item===chosen);button.append(element('small','',`PID ${item.pid} · ${item.duration} 秒 · ${item.time} · ${item.simultaneous?'同一会话':'独立采集'}`));history.append(button);}
  }
  let busy=false,plan=null,activeTarget=null,cancelRequested=false,captureStarted=0;
  function lockControls(value){for(const control of form.querySelectorAll('input,select'))control.disabled=value;for(const button of history.querySelectorAll('button'))button.disabled=value;kernel.disabled=value;user.disabled=value;}
  const device=(operation,payload={})=>hostCall('device-adb',{operation,...payload},{timeout:65000});
  const refresh=ui.button('刷新设备',async()=>{if(busy)return;const previous=devices.value,r=await device('targets');devices.replaceChildren();for(const d of r.targets){const o=new Option(d.label+' · '+d.target,d.target);o.disabled=!d.online;devices.append(o);}if(previous){if(!Array.from(devices.options).some(o=>o.value===previous)){const missing=new Option('原设备已离线，请重新选择',previous);missing.disabled=true;devices.append(missing);}devices.value=previous;}if(!devices.options.length)page.message.update('未发现设备，请确认从机连接','warning');});
  const cancel=ui.button('取消采集',async()=>{if(plan&&activeTarget&&!cancelRequested){cancelRequested=true;page.message.update('正在取消，等待设备确认…','warning');try{const result=await device('exec',{target:activeTarget,argv:['shell',plan.cancel],timeout:10});if(!result.success)throw Error(result.error||result.output||'取消未确认');page.message.update('已发送取消请求，等待采集结束','warning');}catch(error){cancelRequested=false;throw error;}finally{cancel.disabled=cancelRequested;}}});cancel.disabled=true;
  DevMuxPlugin.interval(()=>{if(busy&&captureStarted&&!cancelRequested)page.message.update(`正在采集 · 已等待 ${Math.floor((Date.now()-captureStarted)/1000)} 秒，请保持目标进程运行…`);},1000);
  async function collect(){
    if(busy||!form.reportValidity())return;
    if(!devices.value||devices.selectedOptions[0]?.disabled){page.message.update('请先选择一台在线设备','warning');devices.focus();return;}
    busy=true;devices.disabled=true;refresh.disabled=true;activeTarget=devices.value;raw.clear();plan=null;cancelRequested=false;captureStarted=0;chosen=null;summary.textContent='正在准备采集，图表保留上次结果供参考';
    const args=form.values();
    let completed=false;
    lockControls(true);
    try{
      page.message.update('正在检查设备…');
      const info=await device('exec',{target:activeTarget,argv:['shell','uname -m; id -u'],timeout:10});raw.appendLine(info.stdout||info.output||'');
      if(!info.success||!String(info.stdout).includes('aarch64')||!String(info.stdout).split(/\s+/).includes('0'))throw Error('首版需要 aarch64 Linux 和 root 权限');
      const alive=await device('exec',{target:activeTarget,argv:['shell',`kill -0 ${args.pid}`],timeout:10});if(!alive.success)throw Error('目标进程不存在或无法访问，请刷新 PID 后再试');
      let mapsBefore=null;if(args.mode==='session'||args.mode==='user'){const snapshot=await device('exec',{target:activeTarget,argv:['shell',`cat /proc/${args.pid}/maps; sed 's/.*) //' /proc/${args.pid}/stat | awk '{print $20}'`],timeout:10});if(snapshot.success&&!snapshot.truncated)mapsBefore=snapshot.stdout;}
      const asset=await call('asset',{mode:args.mode});page.message.update('正在部署采集器…');
      const deployment=await device('deploy',{target:activeTarget,...asset});
      plan=await call('plan',{...args,sha256:asset.sha256});
      if(deployment.path!==plan.path)throw Error('部署路径不一致');
      page.message.update('正在采集，请保持目标进程运行…');captureStarted=Date.now();cancel.disabled=false;
      const response=await device('exec',{target:activeTarget,argv:['shell',plan.command],timeout:plan.timeout});
      raw.appendLine(response.stdout||'');raw.appendLine(response.stderr||response.error||'');
      if(!response.success||response.truncated)throw Error(cancelRequested?'采集已取消，可调整参数后重新采集':response.error||response.stderr||response.stdout||'采集失败或输出不完整');
      const result=await call('parse',{mode:args.mode,text:response.stdout});
      captureStarted=0;
      if(args.mode==='session'){
        if(result.session.sessionId!==plan.sessionId||result.session.pid!==args.pid)throw Error('联合采集会话标识或 PID 不匹配');
        renderSession(result.session);showHistogram({id:'本次',time:new Date().toLocaleTimeString(),result});summary.textContent='联合采集已完成，正在解析内核及用户符号…';
        try{await autoSymbols(result,args.pid,mapsBefore);}catch(error){for(const sample of result.session.slowCalls)sample.userSymbols=[];result.session.symbolWarnings=[error.message];}
        if(cancelRequested)throw Error('采集已取消');
      }
      if(args.mode==='kernel'||args.mode==='user'){
        const sample={kernelStack:args.mode==='kernel'?result.frames:null,userStack:args.mode==='user'?result.frames:null},temporary={session:{slowCalls:[sample]}};
        try{await autoSymbols(temporary,args.pid,mapsBefore);}catch(error){temporary.session.symbolWarnings=[error.message];}
        result.symbols=(sample[args.mode+'Symbols']||[]).map(s=>s?.symbol?{name:s.symbol,offset:s.offset}:null);
        result.resolvedFrames=result.symbols.filter(Boolean).length;result.symbolWarnings=temporary.session.symbolWarnings||[];
        if(cancelRequested)throw Error('采集已取消');
      }
      if(args.mode==='latency'||args.mode==='session'){
        try{result.analysis=await call('analyze',{result});}catch(error){raw.appendLine('分析未生成：'+error.message);}
      }
      const record={id:(records[0]?.id||0)+1,target:activeTarget,pid:args.pid,duration:args.duration,mode:args.mode,time:new Date().toLocaleTimeString(),simultaneous:args.mode==='session',result};records.unshift(record);records.splice(30);show(record);completed=true;
      page.message.update('采集完成','success');
    }catch(error){chosen=null;raw.appendLine(error.message);page.message.update(error.message,cancelRequested?'warning':'error');summary.textContent=cancelRequested?'已取消，本次未保存结果':'本次采集失败，下方图表为上次结果';drawer.open=true;}
    finally{captureStarted=0;cancel.disabled=true;if(plan)try{const cleaned=await device('exec',{target:activeTarget,argv:['shell',`rm -f ${plan.path} ${plan.ticket}`],timeout:10});if(!cleaned.success)raw.appendLine('临时文件清理未确认：'+(cleaned.error||cleaned.output||''));}catch(error){raw.appendLine('临时文件清理未确认：'+error.message);}busy=false;devices.disabled=false;refresh.disabled=false;plan=null;lockControls(false);}
    return completed;
  }
  const run=ui.button('开始采集',collect,{variant:'primary'});
  const progress=element('p','ebpf-note ebpf-progress','一键诊断在同一时段采集延迟分布和慢调用双栈，自动解析内核及用户符号；慢调用阈值 2 ms。');
  const collapse=ui.button('收起配置',()=>{shell.classList.toggle('collapsed');collapse.textContent=shell.classList.contains('collapsed')?'展开配置':'收起配置';});
  const kernel=ui.button('内核态',()=>{stackMode='kernel';if(!chosen?.result.session)form.control('mode').value='kernel';showStack();}),user=ui.button('用户态',()=>{stackMode='user';if(!chosen?.result.session)form.control('mode').value='user';showStack();}),tabs=element('div','ebpf-tabs');tabs.append(kernel,user);
  sidebar.append(element('h2','','采集配置'),form,run,cancel,progress,element('h2','','采集记录'),history);history.append(element('p','ebpf-note','当前窗口保留最近 30 次成功采集'));
  histogram.append(element('h2','','系统调用延迟分布'),view);stack.append(element('h2','','最近调用栈'),element('span','ebpf-badge','符号未解析'),tabs,stackBody);panels.append(histogram,stack);
  const drawer=element('details','ebpf-raw'),drawerTitle=element('summary','','原始输出'),actions=element('div','ebpf-raw-actions');
  async function copy(text){try{await navigator.clipboard.writeText(text);}catch{const input=element('textarea');input.value=text;document.body.append(input);input.select();const ok=document.execCommand('copy');input.remove();if(!ok)throw Error('复制被浏览器阻止，请在导出窗口手动复制');}page.message.update('已复制','success');}
  actions.append(ui.button('复制原始输出',()=>copy(raw.textContent)),ui.button('导出结果',()=>{const area=element('textarea','ebpf-export');area.readOnly=true;area.value=JSON.stringify(chosen&&same(chosen)?chosen:{},null,2);const content=element('div');content.append(element('p','','复制以下 JSON 保存为文件。'),area,ui.button('复制 JSON',()=>copy(area.value)));ui.dialog({title:'导出采集结果',content}).show();}));drawer.append(drawerTitle,actions,raw);
  toolbar.prepend(collapse);toolbar.append(devices,refresh);workspace.append(toolbar,summary,panels,analysisPanel,correlatedPanel,drawer,element('p','ebpf-note','联合诊断的慢调用双栈来自同一入口事件；单项模式的栈属于独立采集。用户栈展开依赖帧指针；未解析地址保留原值。'));shell.append(sidebar,workspace);page.body.append(shell);
  function clearViews(){chosen=null;blank(view,'选择“延迟分布”并开始采集后显示直方图');blank(stackBody,'选择内核态或用户态，然后点击“开始采集”');summary.textContent='等待采集';raw.clear();showAnalysis(null);}
  listen(form.control('mode'),'change',()=>{const mode=form.control('mode').value;if(mode==='kernel'||mode==='user'){stackMode=mode;showStack();}page.message.update('已选择'+form.control('mode').selectedOptions[0].textContent+'，点击“开始采集”执行');});
  listen(devices,'change',clearViews);listen(form.control('pid'),'input',clearViews);clearViews();
  refresh.click();
})();
