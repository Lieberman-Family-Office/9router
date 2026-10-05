import {loadUserToken} from '@namespacelabs/sdk/auth';
import {createDevboxClient} from '@namespacelabs/sdk/devbox';
const client=createDevboxClient({tokenSource:loadUserToken});
const box=await client.devboxes.get('2tc0b2eg4mveo',{timeoutMs:20000});
if(box.info.id!=='2tc0b2eg4mveo'||box.info.name!=='9router-qualify-recovery')throw new Error('Devbox identity mismatch');
if(process.argv[2]==='stop')await box.stop({timeoutMs:90000});
const actual=await client.devboxes.get(box.info.id,{timeoutMs:20000});
console.log(JSON.stringify({checkedAt:new Date().toISOString(),id:actual.info.id,name:actual.info.name,state:actual.info.state,instanceId:actual.info.instanceId??null,sdkVersion:'1.4.0'}));
if(process.argv[2]==='verify'&&(actual.info.state!=='stopped'||actual.info.instanceId))process.exitCode=2;
