const {ui,call}=DevMuxPlugin;
const page=ui.mount({title:"插件开发示例",description:'使用公共组件开发插件'});
const form=ui.form([{name:'text',label:'内容',type:'text',default:'Hello DevMux',required:true}]);
const output=ui.log({limit:100});
page.body.append(form,ui.button('调用后端',async()=>{
  if(!form.reportValidity())return;
  const result=await call('echo',form.values());
  output.appendLine(result.text);
},{variant:'primary'}),output);
