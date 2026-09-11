const params=new URLSearchParams(location.search);
const state=params.get('state')||'starting';
const detail=params.get('detail')||'Preparing the bundled Python service and automation browser…';
const content={
  starting:['Starting your private workspace',false],
  restarting:['Recovering the local service',false],
  failed:['Dola Gateway needs attention',true]
};
const selected=content[state]||content.failed;
document.getElementById('title').textContent=selected[0];
document.getElementById('detail').textContent=detail;
document.getElementById('actions').hidden=!selected[1];
document.getElementById('progress').hidden=selected[1];
