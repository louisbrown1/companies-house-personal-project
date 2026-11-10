const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const script = fs.readFileSync('agent/agent_ui/index.html','utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const queue = [], elements = new Map();
const sandbox = {
  document: {getElementById(id) {
    if (!elements.has(id)) elements.set(id,{textContent:'',value:'',style:{},addEventListener(){},scrollIntoView(){}});
    return elements.get(id);
  }},
  localStorage:{getItem(){return 'test-session-12345678'},setItem(){}},
  sessionStorage:{getItem(){return null}}, Headers, AbortSignal,
  window:{prompt(){return null}}, setTimeout(fn){fn()}, setInterval(){}, clearInterval(){},
  fetch:async()=>{
    assert.ok(queue.length,'Unexpected request');
    const item=queue.shift();
    if(item instanceof Error)throw item;
    return {ok:true,status:200,text:async()=>JSON.stringify(item)};
  }
};
vm.runInNewContext(script.replace('  restoreReport();','')+'\nglobalThis.checkPoll=waitForReport;',sandbox);
(async()=>{
  queue.push(new TypeError('Failed to fetch'),{status:'running'},{status:'completed',report:{response:'Recovered report'}});
  assert.equal((await sandbox.checkPoll()).response,'Recovered report');
  queue.push({status:'failed',error:'Report generation reached its time limit'});
  await assert.rejects(sandbox.checkPoll(),/time limit/);
  queue.push({status:'idle'},{status:'idle'},{status:'idle'});
  await assert.rejects(sandbox.checkPoll(),/No report job is running/);
  assert.equal(queue.length,0);
  console.log('Frontend syntax, reconnect, timeout and idle-state checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
