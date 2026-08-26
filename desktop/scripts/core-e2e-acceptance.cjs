#!/usr/bin/env node
/* eslint-disable no-console */
// Canonical aggregate-only core E2E. It never reads meeting/content/payload data.
const { createHash } = require("node:crypto");
const { existsSync, readFileSync, renameSync, writeFileSync } = require("node:fs");
const { join } = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const { createServer } = require("node:net");
const { DEFAULT_MANIFEST_PATH, backendFor, rendererFor } = require("./installed-release-manifest.cjs");
const { descendantPids } = require("./process-tree.cjs");

const COUNTERS = ["frames", "chunks", "enqueued", "attempts", "http_2xx", "ack", "dropped"];
const DIAGNOSTIC_COUNTERS = ["expired_items", "active_in_flight_max", "recovery_in_flight_max", "global_in_flight_max", "attempt_count", "acknowledged_count", "completed_request_count"];
const REPORT_PATH = "/tmp/echodesk-core-e2e-report.json";
const WINDOW_MS = 25_000;
const POLL_MS = 200;
const FRAME_TIMEOUT_MS = 30_000;
const START_STATE_TIMEOUT_MS = 30_000;
const SESSION_READINESS_TIMEOUT_MS = 30_000;
// Product stop may spend up to 240s draining the renderer partition and then
// up to 45s settling accepted backend receipts before the durable end fence.
const STOP_TIMEOUT_MS = 300_000;
const CDP_PORT = 9335;
const CDP_READY_TIMEOUT_MS = 15_000;
const BACKEND_READY_TIMEOUT_MS = 45_000;
const DEFAULT_APP_PATH = "/Applications/EchoDesk.app";
const FORMAL_TOGGLE_SELECTOR = '[data-testid="meeting-status-bar"]';
const MANUAL_START_PATH = "/meetings/manual_start";
const SESSION_BOOTSTRAP_PATHS = new Set(["/bootstrap","/session/enroll","/session/renew","/session/credential/rotate"]);
const SNAPSHOT = `(async()=>{const a=window.__echoCaptureDiagnostics?.(),t=window.__echoCaptureTransportDiagnostics?.();if(!a||!t)return null;const has=(key)=>Object.prototype.hasOwnProperty.call(t,key),optional=(key)=>has(key)?t[key]:null,optionalCode=(key)=>has(key)?t[key]:"unknown";let p="unavailable",m="unavailable";try{p=(await navigator.permissions.query({name:"microphone"})).state}catch{}try{m=await window.echo?.getMicStatus?.()??"unavailable"}catch{}return {main_tcc:m,renderer_permission:p,capture_state:a.captureState,app_track_live:a.appTrackLive===true,audio_context_state:a.audioContextState,explicit_fake:a.fakeMedia===true,last_error_code:a.lastErrorCode??a.last_error_code??"none",frames:a.realFrames,chunks:a.chunkProduced,enqueued:t.enqueued,attempts:t.attemptCount,http_2xx:t.acknowledgedCount,ack:t.acknowledged,dropped:t.droppedBackpressure,transport_ready:has("transportReady")&&typeof t.transportReady==="boolean"?t.transportReady:null,queue_depth:optional("queueDepth"),global_queue_depth:optional("globalQueueDepth"),staging_items:optional("stagingItems"),staging_bytes:optional("stagingBytes"),recovering:optional("recovering"),quarantined_foreign_fence_items:optional("quarantinedForeignFenceItems"),expired_items:optional("expiredItems"),consecutive_failures:optional("consecutiveFailures"),last_http_status:optional("lastHttpStatus"),transport_last_error_code:optionalCode("lastErrorCode"),active_in_flight_current:optional("activeInFlightCurrent"),active_in_flight_max:optional("activeInFlightMax"),recovery_in_flight_current:optional("recoveryInFlightCurrent"),recovery_in_flight_max:optional("recoveryInFlightMax"),global_in_flight_current:optional("globalInFlightCurrent"),global_in_flight_max:optional("globalInFlightMax"),attempt_count:optional("attemptCount"),acknowledged_count:optional("acknowledgedCount"),completed_request_count:optional("completedRequestCount"),last_enqueue_reject_reason:optionalCode("lastEnqueueRejectReason"),active_partition_item_count:optional("activePartitionItemCount"),active_partition_byte_count:optional("activePartitionByteCount"),active_partition_max_items:optional("activePartitionMaxItems"),active_partition_max_bytes:optional("activePartitionMaxBytes"),global_item_count:optional("globalItemCount"),global_byte_count:optional("globalByteCount"),global_max_items:optional("globalMaxItems"),global_max_bytes:optional("globalMaxBytes"),partition_count:optional("partitionCount")}})()`;
const CONTROL = `(()=>{const b=document.querySelector('${FORMAL_TOGGLE_SELECTOR}');return {selector_found:!!b,disabled:!!b?.disabled,aria_pressed:b?.getAttribute('aria-pressed')==='true'}})()`;
const FORMAL_CONTROL_RECT = `(()=>{const b=document.querySelector('${FORMAL_TOGGLE_SELECTOR}');if(!b)return {visible_bool:false};const r=b.getBoundingClientRect(),s=getComputedStyle(b),visible=r.width>0&&r.height>0&&s.display!=="none"&&s.visibility!=="hidden"&&Number(s.opacity)!==0;return {visible_bool:visible,x:r.left+r.width/2,y:r.top+r.height/2}})()`;
const FORMAL_CLICK_PROBE_RESET = `(()=>{const b=document.querySelector('${FORMAL_TOGGLE_SELECTOR}');if(!b)return false;const key='__echodeskFormalInputProofV1',previous=window[key];if(previous?.controller)previous.controller.abort();const state={click_event_count:0,trusted_event_count:0,controller:new AbortController()};b.addEventListener('click',(event)=>{state.click_event_count+=1;if(event.isTrusted===true)state.trusted_event_count+=1;},{capture:true,signal:state.controller.signal});window[key]=state;return true})()`;
const FORMAL_CLICK_PROBE_COUNTS = `(()=>{const state=window.__echodeskFormalInputProofV1;return {click_event_count:Number(state?.click_event_count||0),trusted_event_count:Number(state?.trusted_event_count||0)}})()`;
const RENDER_TICK = "new Promise((resolve)=>requestAnimationFrame(()=>resolve(true)))";
function rendererSessionReadinessExpression(timeoutMs = SESSION_READINESS_TIMEOUT_MS) {
  const boundedTimeout = Number.isSafeInteger(timeoutMs) && timeoutMs > 0
    ? timeoutMs
    : SESSION_READINESS_TIMEOUT_MS;
  return `(async()=>{const root=document.documentElement;const current=()=>({renderer_backend_ready_bool:root.dataset.backendReachability==="reachable"&&root.dataset.backendApiContract==="compatible",renderer_session_ready_bool:root.dataset.sessionIdentity==="ready"});const ready=(value)=>value.renderer_backend_ready_bool&&value.renderer_session_ready_bool;let value=current();if(!ready(value)){value=await new Promise((resolve)=>{let settled=false;const finish=()=>{if(settled)return;const next=current();if(!ready(next))return;settled=true;observer.disconnect();clearTimeout(timer);resolve(next)};const observer=new MutationObserver(finish);observer.observe(root,{attributes:true,attributeFilter:["data-backend-reachability","data-backend-api-contract","data-session-identity"]});const timer=setTimeout(()=>{if(settled)return;settled=true;observer.disconnect();resolve(current())},${boundedTimeout});finish()})}if(!ready(value))return {...value,backend_2xx_bool:false};let backend_2xx_bool=false;try{const base=window.echo?.backendHost;if(typeof base==="string"&&base.length>0){const response=await fetch(new URL("/bootstrap",base),{cache:"no-store",redirect:"error"});backend_2xx_bool=response.status>=200&&response.status<300;try{await response.body?.cancel?.()}catch{}}}catch{}return {...value,backend_2xx_bool}})()`;
}
function fail(code) { const error = new Error(code); error.code = code; throw error; }
function nonNegative(value, field) { if (!Number.isSafeInteger(value) || value < 0) fail(`invalid_${field}`); return value; }
function optionalNonNegative(value, field) { return value===null||value===undefined?null:nonNegative(value,field); }
function machineCode(value, fallback="unavailable") { return typeof value==="string"&&/^[a-z0-9_.:-]{1,64}$/i.test(value)&&!/(^|[_.:-])(id|key|url|content|payload|credential|token|cookie|header|body|path)([_.:-]|$)/i.test(value)?value:fallback; }
function optionalHttpStatus(value) { return value===null||value===undefined?null:(Number.isInteger(value)&&value>=100&&value<=599?value:null); }
function enqueueRejectReason(value) { return value===null?null:["count_limit","byte_limit","global_count_limit","global_byte_limit"].includes(value)?value:"unknown"; }
function snapshot(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail("snapshot_unavailable");
  const out = { main_tcc: machineCode(value.main_tcc), renderer_permission: machineCode(value.renderer_permission), capture_state: machineCode(value.capture_state), app_track_live: value.app_track_live === true, audio_context_state: machineCode(value.audio_context_state), explicit_fake: value.explicit_fake === true, last_error_code: machineCode(value.last_error_code) };
  for (const key of COUNTERS) out[key] = nonNegative(value[key], key);
  for (const key of ["queue_depth","global_queue_depth","staging_items","staging_bytes","quarantined_foreign_fence_items","expired_items","consecutive_failures","active_in_flight_current","active_in_flight_max","recovery_in_flight_current","recovery_in_flight_max","global_in_flight_current","global_in_flight_max","attempt_count","acknowledged_count","completed_request_count","active_partition_item_count","active_partition_byte_count","active_partition_max_items","active_partition_max_bytes","global_item_count","global_byte_count","global_max_items","global_max_bytes","partition_count"]) out[key]=optionalNonNegative(value[key],key);
  out.recovering=typeof value.recovering==="boolean"?value.recovering:null;
  out.transport_ready=typeof value.transport_ready==="boolean"?value.transport_ready:null;
  out.last_http_status=optionalHttpStatus(value.last_http_status);
  out.transport_last_error_code=value.transport_last_error_code===null?null:machineCode(value.transport_last_error_code,"unknown");
  out.last_enqueue_reject_reason=enqueueRejectReason(value.last_enqueue_reject_reason);
  return out;
}
function physical(s) { return s.main_tcc === "granted" && s.renderer_permission === "granted" && s.capture_state === "capturing" && s.app_track_live && s.audio_context_state === "running" && !s.explicit_fake; }
function durableAdmissionProof(before,after,enqueuedDelta) { const legacy=before.active_partition_item_count===null||before.active_partition_item_count===undefined||after.active_partition_item_count===null||after.active_partition_item_count===undefined,activeDelta=legacy?null:Math.max(0,after.active_partition_item_count-before.active_partition_item_count);if(enqueuedDelta>0)return {durable_admission_delta:enqueuedDelta,durable_admission_source:legacy?"legacy_enqueued_counter":"enqueued_counter"};if(activeDelta>0)return {durable_admission_delta:activeDelta,durable_admission_source:"active_partition_items"};return {durable_admission_delta:0,durable_admission_source:legacy?"legacy_enqueued_counter":"none"}; }
function deltas(before, after) { const out={}; for (const key of COUNTERS) { if (after[key] < before[key]) fail(`regressed_${key}`); out[key]=after[key]-before[key]; } for(const key of DIAGNOSTIC_COUNTERS){if(before[key]===null||before[key]===undefined||after[key]===null||after[key]===undefined){out[key]=null;continue;}if(after[key]<before[key])fail(`regressed_${key}`);out[key]=after[key]-before[key];}return Object.assign(out,durableAdmissionProof(before,after,out.enqueued)); }
function firstBreak(d) { return ["frames","chunks"].find((key)=>d[key]===0) || ((d.durable_admission_delta??d.enqueued)===0?"enqueued":null) || ["attempts","http_2xx","ack"].find((key)=>d[key]===0) || (d.dropped !== 0 ? "dropped" : null); }
function writeReport(report, path = REPORT_PATH) { const temp=`${path}.${process.pid}.tmp`; writeFileSync(temp, JSON.stringify(report)); renameSync(temp, path); }
function report(phase, fields={}) { const deltaValue=fields.deltas??null;return { schema:"echodesk-core-e2e-v2", phase, first_break: fields.first_break ?? null, error_code: fields.error_code ?? null, cleanup_phase:fields.cleanup_phase ?? null, cleanup_first_break:fields.cleanup_first_break ?? null, cleanup_error:fields.cleanup_error ?? null, transport_ready:fields.transport_ready??fields.post?.transport_ready??fields.first?.transport_ready??fields.pre?.transport_ready??null, durable_admission_delta:fields.durable_admission_delta??deltaValue?.durable_admission_delta??null, durable_admission_source:fields.durable_admission_source??deltaValue?.durable_admission_source??null, pre:fields.pre ?? null, first:fields.first ?? null, post:fields.post ?? null, deltas:deltaValue, aggregates:fields.aggregates ?? null, start:fields.start ?? null, start_settlement:fields.start_settlement ?? null, start_settlement_at_timeout:fields.start_settlement_at_timeout ?? null, start_settlement_final:fields.start_settlement_final ?? null, response_after_timeout:fields.response_after_timeout===true, stop:fields.stop ?? null, stop_settlement:fields.stop_settlement ?? null, readiness:fields.readiness ?? null, session_readiness:fields.session_readiness ?? null, database:fields.database ?? null, transition:fields.transition ?? null, start_network:fields.start_network ?? null }; }
function primaryFirstBreak(code, phase) { if(COUNTERS.includes(code))return code;if(["session_readiness","manual_start_http","manual_start_state"].includes(code))return code;if(code==="stop_settlement"||phase==="stop_settle")return "stop_settlement";if(phase==="first_frame"||phase==="window")return "first_frame";return null; }
function executableFor(app) { if(typeof app!=="string"||app.length===0)fail("app_required"); return join(app,"Contents","MacOS","EchoDesk"); }
function verifyInstalled(app, sha) { const exe=executableFor(app); if(!existsSync(exe)||!/^[a-f0-9]{64}$/i.test(sha)||createHash("sha256").update(readFileSync(exe)).digest("hex")!==sha.toLowerCase()) fail("installed_hash"); return exe; }
function releaseManifest(manifestPath=DEFAULT_MANIFEST_PATH) { let value; try { value=JSON.parse(readFileSync(manifestPath,"utf8")); } catch { fail("release_manifest"); } const fields=["version","build","executable_sha256","renderer_sha256","backend_sha256"]; if(!value||typeof value!=="object"||Array.isArray(value)||Object.keys(value).length!==fields.length||fields.some((field)=>!Object.prototype.hasOwnProperty.call(value,field)))fail("release_manifest"); if(!/^[^\0\r\n]{1,80}$/.test(String(value.version))||!/^[^\0\r\n]{1,80}$/.test(String(value.build))||fields.slice(2).some((field)=>!/^[a-f0-9]{64}$/i.test(String(value[field]))))fail("release_manifest"); return Object.fromEntries(fields.map((field)=>[field,field.endsWith("_sha256")?String(value[field]).toLowerCase():String(value[field])])); }
function verifyInstalledResources(app, manifest) { const fields={executable_sha256:executableFor(app),renderer_sha256:rendererFor(app),backend_sha256:backendFor(app)}; for(const [field,resource] of Object.entries(fields)){if(!existsSync(resource)||createHash("sha256").update(readFileSync(resource)).digest("hex")!==manifest[field])fail("installed_hash");} return Object.keys(fields).length; }
function resolveInstalledBundle({appPath=DEFAULT_APP_PATH,manifestPath=DEFAULT_MANIFEST_PATH}={}) { const manifest=releaseManifest(manifestPath); verifyInstalledResources(appPath,manifest); return {app:appPath,sha256:manifest.executable_sha256,manifest}; }
function openSqliteFiles(rootPid, {listProcesses, listOpenFiles=(pids)=>spawnSync("lsof",["-Fpn","-p",pids.join(",")],{encoding:"utf8"})}={}) { const pids=descendantPids(rootPid,{listProcesses}),fileOwners=new Map();let currentPid=null;for(const line of String(listOpenFiles(pids)?.stdout||"").split(/\r?\n/)){if(/^p\d+$/.test(line)){currentPid=Number(line.slice(1));continue;}if(!line.startsWith("n")||!/\.db$/i.test(line.slice(1)))continue;const file=line.slice(1),owners=fileOwners.get(file)||new Set();if(Number.isSafeInteger(currentPid)&&currentPid>0)owners.add(currentPid);fileOwners.set(file,owners);}return {pids,files:[...fileOwners.keys()],fileOwners}; }
function hasMeetingsSchema(db, {pragma=(candidate)=>spawnSync("sqlite3",["-readonly","-noheader",candidate,"PRAGMA table_info(meetings);"],{encoding:"utf8"})}={}) { const result=pragma(db); return result?.status===0&&String(result.stdout||"").trim().length>0; }
function processTopology(opened,matches,dependencies={}) {
  const tree=new Set(opened.pids??[]),owners=opened.fileOwners??new Map();
  const runtime=new Set(dependencies.runtimePids??matches.flatMap((file)=>[...(owners.get(file)??[])]));
  let runtimeCount=runtime.size;
  if(matches.length===1&&runtimeCount===0)runtimeCount=1;
  let listenerPids=dependencies.listenerPids;
  if(!listenerPids&&runtime.size>0){
    const list=dependencies.listListeningPids??((pids)=>spawnSync("lsof",["-tiTCP","-sTCP:LISTEN","-a","-p",pids.join(",")],{encoding:"utf8"}));
    listenerPids=String(list([...runtime])?.stdout||"").trim().split(/\s+/).map(Number).filter((pid)=>runtime.has(pid));
  }
  const listeners=new Set(listenerPids??[]);
  let supervisorPids=dependencies.supervisorPids;
  if(!supervisorPids){
    const readParent=dependencies.parentPid??((pid)=>spawnSync("ps",["-o","ppid=","-p",String(pid)],{encoding:"utf8"}));
    supervisorPids=[...runtime].map((pid)=>Number(readParent(pid)?.stdout)).filter((pid)=>tree.has(pid)&&!runtime.has(pid));
  }
  const supervisors=new Set(supervisorPids);
  const logical=matches.length===1?1:0;
  return {logical_backend_count:logical,supervisor_count:supervisors.size,runtime_child_count:runtimeCount,listener_count:listeners.size,process_tree_pid_count:tree.size,backend_pid_count:logical,backend_pid_count_deprecated_bool:true};
}
function inspectOpenMeetingDb(rootPid, dependencies={}) { const opened=dependencies.candidates?{pids:dependencies.pids??[],files:dependencies.candidates,fileOwners:dependencies.fileOwners??new Map()}:openSqliteFiles(rootPid,dependencies); const matches=opened.files.filter((candidate)=>hasMeetingsSchema(candidate,dependencies)),topology=processTopology(opened,matches,dependencies),ready=matches.length===1&&topology.logical_backend_count===1&&topology.listener_count===1; return { db:matches.length===1?matches[0]:null, error_code:matches.length>1?"database_ambiguous":null, database:{candidate_count:opened.files.length,...topology,ready_bool:ready} }; }
function resolveOpenMeetingDb(rootPid, dependencies={}) { const value=inspectOpenMeetingDb(rootPid,dependencies); if(value.error_code)fail(value.error_code); if(!value.db)fail("database_missing"); return value.db; }
function failWithDatabase(code, value) { const error=new Error(code); error.code=code; error.database=value; throw error; }
async function waitForMeetingDb(rootPid, options={}) { const inspect=options.inspect||inspectOpenMeetingDb, wait=options.delay||delay, now=options.now||Date.now, timeout=options.timeout??BACKEND_READY_TIMEOUT_MS; const deadline=now()+timeout; let value={candidate_count:0,logical_backend_count:0,supervisor_count:0,runtime_child_count:0,listener_count:0,process_tree_pid_count:0,backend_pid_count:0,backend_pid_count_deprecated_bool:true,ready_bool:false,poll_count:0}; while(now()<deadline){const pollCount=value.poll_count+1,inspected=inspect(rootPid,options);value={...value,...inspected.database,poll_count:pollCount};if(inspected.error_code)failWithDatabase(inspected.error_code,value);if(inspected.db&&value.ready_bool===true)return {db:inspected.db,database:value};await wait(POLL_MS);}failWithDatabase("database_missing",value); }
function sqliteCounts(db) { const q="SELECT COUNT(*) FROM meetings;SELECT COUNT(*) FROM meeting_segments;SELECT COUNT(*) FROM meetings WHERE minutes_json IS NOT NULL;SELECT COUNT(*) FROM artifacts;SELECT COUNT(*) FROM hub_sync_outbox WHERE state NOT IN ('applied','duplicate');SELECT COUNT(*) FROM pragma_foreign_key_check"; const r=spawnSync("sqlite3",["-readonly","-noheader",db,q],{encoding:"utf8"}); if(r.status!==0)fail("aggregate_database"); const v=r.stdout.trim().split(/\s+/).map(Number); if(v.length!==6)fail("aggregate_database"); return Object.fromEntries(["meetings","segments","summaries","artifacts","outbox","foreign_key_violations"].map((k,i)=>[k,nonNegative(v[i],k)])); }
function activeMeetings(db) { const r=spawnSync("sqlite3",["-readonly","-noheader",db,"SELECT COUNT(*) FROM meetings WHERE state = 'in_meeting'"],{encoding:"utf8"}); if(r.status!==0)fail("aggregate_database"); const values=r.stdout.trim().split(/\s+/).map(Number); if(values.length!==1)fail("aggregate_database"); return nonNegative(values[0],"in_meeting"); }
function transitionAggregate(startedAt=Date.now()) { return { startedAt,pollTotal:0,durationMs:0,captureStateCounts:{},lastErrorCodeCounts:{},trackLiveCounts:{},audioContextCounts:{},inMeetingCounts:{},buttonDisabledCounts:{},ariaPressedCounts:{}}; }
function count(map,key) { map[String(key)]=(map[String(key)]||0)+1; }
function recordTransition(transition,s,inMeeting,control) { transition.pollTotal+=1;count(transition.captureStateCounts,s.capture_state);count(transition.lastErrorCodeCounts,s.last_error_code);count(transition.trackLiveCounts,s.app_track_live);count(transition.audioContextCounts,s.audio_context_state);count(transition.inMeetingCounts,inMeeting);count(transition.buttonDisabledCounts,control.disabled===true);count(transition.ariaPressedCounts,control.aria_pressed===true); }
function finishTransition(transition) { transition.durationMs=Math.max(0,Date.now()-transition.startedAt); const {startedAt,...safe}=transition;return safe; }
function startNetworkAggregate() { return { requests:new Map(),currentGeneration:0,manualTransaction:null,start_request_count:0,response_status_counts:{},network_error_class_counts:{},session_request_count:0,session_status_counts:{},session_error_class_counts:{} }; }
function isManualStartRequest(request) { if(!request||request.method!=="POST"||typeof request.url!=="string")return false; try { return new URL(request.url).pathname===MANUAL_START_PATH; } catch { return false; } }
function isSessionBootstrapRequest(request) { if(!request||typeof request.method!=="string"||typeof request.url!=="string")return false; try { return SESSION_BOOTSTRAP_PATHS.has(new URL(request.url).pathname); } catch { return false; } }
function safeResponseStatus(value) { return Number.isInteger(value)&&value>=100&&value<=599?String(value):null; }
function networkErrorClass(params) { if(typeof params?.blockedReason==="string"&&params.blockedReason.length>0)return "blocked"; const value=String(params?.errorText||"").toLowerCase(); if(value.includes("timed out")||value.includes("timeout"))return "timeout"; if(value.includes("dns")||value.includes("name not resolved"))return "dns"; if(value.includes("connection")||value.includes("network")||value.includes("reset")||value.includes("refused")||value.includes("closed"))return "connection"; return "other"; }
function recordStartNetworkEvent(network, message) {
  const params=message?.params,requestId=params?.requestId;
  if(message?.method==="Network.requestWillBeSent"){
    if(typeof requestId!=="string")return;
    const kind=isManualStartRequest(params.request)?"manual":isSessionBootstrapRequest(params.request)?"session":null;
    if(!kind||network.requests.has(requestId))return;
    network.requests.set(requestId,{kind,generation:network.currentGeneration,status:null,settled:false,errorClass:null});
    if(kind==="manual")network.start_request_count+=1;
    else network.session_request_count+=1;
    return;
  }
  if(typeof requestId!=="string")return;
  const request=network.requests.get(requestId);
  if(!request)return;
  if(message?.method==="Network.responseReceived"){
    const status=safeResponseStatus(params?.response?.status);
    if(!status||request.settled)return;
    request.status=Number(status);
    // Response headers are the canonical HTTP settlement boundary. Waiting
    // for loadingFinished created a second, renderer-dependent completion
    // path even though fetch had already received the authoritative status.
    request.settled=true;
    if(request.kind==="manual")count(network.response_status_counts,status);
    else count(network.session_status_counts,status);
    return;
  }
  if(message?.method==="Network.loadingFinished"){
    if(request.settled)return;
    request.settled=true;
    return;
  }
  if(message?.method==="Network.loadingFailed"){
    if(request.settled)return;
    const kind=networkErrorClass(params);
    request.settled=true;
    request.errorClass=kind;
    if(request.kind==="manual")count(network.network_error_class_counts,kind);
    else {
      count(network.session_error_class_counts,kind);
      // The readiness confirmation deliberately cancels its response body.
      // Once response headers proved 2xx, that cancellation is not a transport failure.
      if(request.status!==null&&request.status>=200&&request.status<300)request.errorClass=null;
    }
  }
}
function safeStatusCounts(values, field) { return Object.fromEntries(Object.entries(values??{}).filter(([status])=>/^\d{3}$/.test(status)&&safeResponseStatus(Number(status))===status).map(([status,value])=>[status,nonNegative(value,`${field}_${status}`)])); }
function safeErrorCounts(values, field) { return Object.fromEntries(Object.entries(values??{}).filter(([kind])=>["blocked","connection","dns","timeout","other"].includes(kind)).map(([kind,value])=>[kind,nonNegative(value,`${field}_${kind}`)])); }
function startNetworkReport(network, transition) { return { start_request_count:nonNegative(network?.start_request_count??0,"start_request_count"), response_status_counts:safeStatusCounts(network?.response_status_counts,"response_status"), network_error_class_counts:safeErrorCounts(network?.network_error_class_counts,"network_error"), session_request_count:nonNegative(network?.session_request_count??0,"session_request_count"), session_status_counts:safeStatusCounts(network?.session_status_counts,"session_status"), session_error_class_counts:safeErrorCounts(network?.session_error_class_counts,"session_error"), state_transition:{ in_meeting_0:nonNegative(transition?.inMeetingCounts?.["0"]??0,"in_meeting_0"), in_meeting_1:nonNegative(transition?.inMeetingCounts?.["1"]??0,"in_meeting_1") } }; }
function generationRequests(network,kind,generation) { return [...(network?.requests?.values?.()??[])].filter((request)=>request.kind===kind&&request.generation===generation); }
function generationRequestState(network,kind,generation) { const requests=generationRequests(network,kind,generation),statuses=requests.map((request)=>request.status).filter((status)=>status!==null);return {request_count:requests.length,pending_count:requests.filter((request)=>!request.settled).length,http_2xx_count:statuses.filter((status)=>status>=200&&status<300).length,http_failure_count:statuses.filter((status)=>status<200||status>=300).length,network_failure_count:requests.filter((request)=>request.errorClass!==null).length}; }
function sessionNetworkRecovered(network) { const state=generationRequestState(network,"session",network.currentGeneration);return state.http_2xx_count>0; }
function markSessionReadinessBoundary(network) { network.currentGeneration+=1;network.manualTransaction={generation:network.currentGeneration};return network.manualTransaction.generation; }
function normalizedSessionReadiness(value, network) { const candidate=value&&typeof value==="object"&&!Array.isArray(value)?value:{}; return { backend_2xx_bool:candidate.backend_2xx_bool===true&&sessionNetworkRecovered(network), renderer_backend_ready_bool:candidate.renderer_backend_ready_bool===true, renderer_session_ready_bool:candidate.renderer_session_ready_bool===true }; }
function sessionReadinessComplete(value) { return value.backend_2xx_bool&&value.renderer_backend_ready_bool&&value.renderer_session_ready_bool; }
function failWithSessionReadiness(value) { const error=new Error("session_readiness");error.code="session_readiness";error.session_readiness=value;throw error; }
async function waitForSessionReadiness(connection, network, options={}) { const wait=options.waitForRendererSession||((target,timeout)=>target.evaluate(rendererSessionReadinessExpression(timeout))); let candidate;try{candidate=await wait(connection,options.timeout??SESSION_READINESS_TIMEOUT_MS);}catch{failWithSessionReadiness(normalizedSessionReadiness(null,network));}const value=normalizedSessionReadiness(candidate,network);if(!sessionReadinessComplete(value))failWithSessionReadiness(value);return value; }
function firstFrameDecision(pre,candidate) { if(candidate.last_error_code!=="none"&&candidate.last_error_code!=="unavailable")return "capture_rejected"; return physical(candidate)&&candidate.frames>pre.frames?"ready":"pending"; }
function inputProof(value={}) { const click_event_count=nonNegative(value.click_event_count??0,"click_event_count"),trusted_event_count=nonNegative(value.trusted_event_count??0,"trusted_event_count");if(trusted_event_count>click_event_count)fail("input_proof");return {click_event_count,trusted_event_count}; }
function inputAction(visible, proof={}) { return {action:"input_dispatch",visible_bool:visible===true,...inputProof(proof)}; }
function inputPoint(value) { return value?.visible_bool===true&&Number.isFinite(value.x)&&Number.isFinite(value.y)&&value.x>0&&value.y>0?{x:value.x,y:value.y}:null; }
async function dispatchFormalInput(connection, code="toggle_input_target") { await connection.command("Page.bringToFront");await connection.evaluate(FORMAL_CLICK_PROBE_RESET);const point=inputPoint(await connection.evaluate(FORMAL_CONTROL_RECT));if(!point){const error=new Error(code);error.code=code;error.input_action=inputAction(false);throw error;}for(const params of [{type:"mouseMoved",button:"none",buttons:0},{type:"mousePressed",button:"left",buttons:1,clickCount:1},{type:"mouseReleased",button:"left",buttons:0,clickCount:1}])await connection.command("Input.dispatchMouseEvent",{...params,x:point.x,y:point.y});return inputAction(true,inputProof(await connection.evaluate(FORMAL_CLICK_PROBE_COUNTS))); }
async function prepareTrustedStart(connection, network, options={}) { const wait=options.waitForReadiness||waitForSessionReadiness;const session_readiness=await wait(connection,network,options.sessionReadinessOptions);markSessionReadinessBoundary(network);const readBaseline=options.readBaseline||((target)=>target.evaluate(SNAPSHOT));const pre=snapshot(await readBaseline(connection));options.onBeforeInput?.();const dispatch=options.dispatchInput||dispatchFormalInput;try{return {session_readiness,pre,start:await dispatch(connection,"toggle_start_target")};}catch(error){error.session_readiness=session_readiness;error.pre=pre;error.input_action=error.input_action??inputAction(false);throw error;} }
function manualStartGenerationState(network,generation=network?.manualTransaction?.generation) { return generationRequestState(network,"manual",generation); }
function startSettlementReport(state,inMeeting) { return {request_count:nonNegative(state?.request_count??0,"manual_request_count"),pending_count:nonNegative(state?.pending_count??0,"manual_pending_count"),http_2xx_count:nonNegative(state?.http_2xx_count??0,"manual_http_2xx_count"),http_failure_count:nonNegative(state?.http_failure_count??0,"manual_http_failure_count"),network_failure_count:nonNegative(state?.network_failure_count??0,"manual_network_failure_count"),in_meeting_count:nonNegative(inMeeting??0,"in_meeting")}; }
function responseArrivedAfterTimeout(atTimeout,finalValue) { return atTimeout?.pending_count>0&&atTimeout?.http_2xx_count===0&&finalValue?.pending_count===0&&finalValue?.http_2xx_count>0; }
function failWithStartSettlement(code,value,timedOut=false) { const error=new Error(code);error.code=code;error.start_settlement=value;error.start_settlement_timeout=timedOut;throw error; }
async function waitForManualStartSettlement(connection,db,network,options={}) { const now=options.now||Date.now,timeout=options.timeout??START_STATE_TIMEOUT_MS,wait=options.waitForCondition||(()=>delay(POLL_MS)),readActive=options.readActive||activeMeetings,generation=options.generation??network?.manualTransaction?.generation,cleanup=options.cleanup===true,deadline=now()+timeout;let state=manualStartGenerationState(network,generation),inMeeting=readActive(db);while(now()<deadline){state=manualStartGenerationState(network,generation);inMeeting=readActive(db);const reportValue=startSettlementReport(state,inMeeting);if(!cleanup&&(state.http_failure_count>0||state.network_failure_count>0))failWithStartSettlement("manual_start_http",reportValue);if(state.request_count>0&&state.pending_count===0){if(cleanup)return reportValue;if(state.http_2xx_count!==state.request_count)failWithStartSettlement("manual_start_http",reportValue);if(inMeeting===1)return reportValue;}await wait(connection);}const value=startSettlementReport(state,inMeeting);if(cleanup){const error=new Error("stop_settlement");error.code="stop_settlement";error.start_settlement=value;throw error;}if(state.request_count===0||state.pending_count>0||state.http_2xx_count!==state.request_count)failWithStartSettlement("manual_start_http",value,true);failWithStartSettlement("manual_start_state",value,true); }
function stopPlan(inMeeting) { return { state:inMeeting===0?"none":"in_meeting", action:inMeeting===0?"already_stopped":"input_dispatch", visible_bool:inMeeting===0?null:null }; }
function failWithStop(code, stop) { const error=new Error(code);error.code=code;error.stop=stop;throw error; }
function stopComplete(before, after, action) { const stop=stopPlan(before); if(before===0)return stop; if(!action?.visible_bool){stop.visible_bool=false;failWithStop("toggle_stop_target",stop);} if(after!==0){stop.visible_bool=true;failWithStop("toggle_stop_persisted",stop);} return { state:"none", ...action }; }
async function stopMeeting(connection, db) { const before=activeMeetings(db); const initial=stopPlan(before); if(before===0)return initial; let action;try{action=await dispatchFormalInput(connection,"toggle_stop_target");}catch(error){error.stop={...initial,...(error.input_action??inputAction(false))};throw error;} const deadline=Date.now()+STOP_TIMEOUT_MS; let after=before; while(Date.now()<deadline){after=activeMeetings(db);if(after===0)return stopComplete(before,after,action);await new Promise((resolve)=>setTimeout(resolve,POLL_MS));} return stopComplete(before,after,action); }
async function settleStartedMeeting(connection,db,network,options={}) { const readActive=options.readActive||activeMeetings,waitForTransaction=options.waitForTransaction||waitForManualStartSettlement,settled=await waitForTransaction(connection,db,network,{...(options.startOptions||{}),readActive,cleanup:true}),stopActive=options.stopActive||stopMeeting;let active=readActive(db),stop=stopPlan(active);try{if(active>0)stop=await stopActive(connection,db);}catch(error){error.start_settlement_final=settled;throw error;}active=readActive(db);if(settled.pending_count!==0||active!==0){const error=new Error("stop_settlement");error.code="stop_settlement";error.start_settlement_final=settled;error.stop_settlement={pending_count:settled.pending_count,active_count:active};throw error;}return {pending_count:0,active_count:0,start_settlement:settled,stop}; }
function productPages(targets) { return Array.isArray(targets)?targets.filter((target)=>target?.type==="page"&&!String(target.url||"").startsWith("devtools://")&&typeof target.webSocketDebuggerUrl==="string"):[]; }
async function listTargets(endpoint) { return (await (await fetch(`${endpoint}/json/list`)).json()); }
async function connectCdpTarget(target) { const socket=new WebSocket(target.webSocketDebuggerUrl); await new Promise((ok,no)=>{socket.onopen=ok;socket.onerror=no}); let id=0; const pending=new Map(), listeners=new Set(); socket.onmessage=(e)=>{const m=JSON.parse(e.data),p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p.no(new Error("cdp_command")):p.ok(m.result);return;}for(const listener of listeners)listener(m);}; const command=(method,params={})=>new Promise((ok,no)=>{const n=++id;pending.set(n,{ok,no});socket.send(JSON.stringify({id:n,method,params}));}); const evaluate=async(expression)=>{const result=await command("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});return result?.result?.value;}; const enableStartNetworkDiagnostics=async()=>{const aggregate=startNetworkAggregate();listeners.add((message)=>recordStartNetworkEvent(aggregate,message));await command("Network.enable");return aggregate;}; return { socket,command,evaluate,enableStartNetworkDiagnostics }; }
async function inspectProductTarget(target) { const connection=await connectCdpTarget(target); try { const ready_bool=await connection.evaluate("document.readyState === 'complete'"); const diagnostics_bool=await connection.evaluate("Boolean(window.__echoCaptureDiagnostics&&window.__echoCaptureTransportDiagnostics)"); return { connection,ready_bool:ready_bool===true,diagnostics_bool:diagnostics_bool===true }; } catch(error) { connection.socket.close(); throw error; } }
function readiness(target_count=0,ready_bool=false,diagnostics_bool=false,poll_count=0) { return {target_count,ready_bool,diagnostics_bool,poll_count}; }
function failWithReadiness(code, value) { const error=new Error(code);error.code=code;error.readiness=value;throw error; }
async function waitForRendererReady(endpoint, options={}) { const getTargets=options.listTargets||listTargets, inspect=options.inspectProductTarget||inspectProductTarget, wait=options.delay||delay, timeout=options.timeout??CDP_READY_TIMEOUT_MS; const deadline=Date.now()+timeout; let value=readiness(); while(Date.now()<deadline){value.poll_count+=1;try { const pages=productPages(await getTargets(endpoint)); value.target_count=pages.length;if(pages.length>0){const inspected=await inspect(pages[0]);value.ready_bool=inspected.ready_bool;value.diagnostics_bool=inspected.diagnostics_bool;if(value.ready_bool&&value.diagnostics_bool)return {connection:inspected.connection,readiness:value};if(inspected.connection)inspected.connection.socket.close();} } catch{}await wait(POLL_MS);}failWithReadiness("renderer_ready_timeout",value); }
function controlledLaunchArgs(port=CDP_PORT) { return [`--remote-debugging-port=${port}`]; }
function delay(ms) { return new Promise((resolve)=>setTimeout(resolve,ms)); }
async function assertPortAvailable(port) { await new Promise((resolve,reject)=>{const server=createServer();server.once("error",()=>reject(Object.assign(new Error("cdp_port_in_use"),{code:"cdp_port_in_use"})));server.listen(port,"127.0.0.1",()=>server.close(resolve));}); }
async function quitStandardApp() { spawnSync("osascript",["-e",'tell application "EchoDesk" to quit'],{stdio:"ignore"}); const deadline=Date.now()+10_000; while(Date.now()<deadline){if(!mainRunning())return;await delay(POLL_MS);}fail("standard_quit"); }
async function waitForCdp(endpoint, child, timeout=CDP_READY_TIMEOUT_MS) { const deadline=Date.now()+timeout; while(Date.now()<deadline){if(child.exitCode!==null||child.signalCode)fail("app_launch");try{const response=await fetch(`${endpoint}/json/version`);if(response.ok)return;}catch{}await delay(POLL_MS);}fail("cdp_timeout"); }
async function stopControlled(child) { if(!child||child.exitCode!==null)return; child.kill("SIGTERM"); const deadline=Date.now()+5_000; while(child.exitCode===null&&Date.now()<deadline)await delay(POLL_MS); if(child.exitCode===null){child.kill("SIGKILL");await delay(POLL_MS);} }
function mainRunning() { const rows=require("./process-tree.cjs").readProcessTable();return rows.some((row)=>require("node:path").basename(row.comm)==="EchoDesk"); }
async function restoreStandardApp(app) { const opened=spawnSync("open",[app],{stdio:"ignore"}); if(opened.status!==0)fail("standard_restore"); const deadline=Date.now()+10_000; while(Date.now()<deadline){if(mainRunning())return;await delay(POLL_MS);}fail("standard_restore"); }
async function withControlledApp(options, work) { const runtime=options.runtime||{}; const port=options.cdpPort||CDP_PORT; const endpoint=`http://127.0.0.1:${port}`; const quit=runtime.quitStandardApp||quitStandardApp, assertAvailable=runtime.assertPortAvailable||assertPortAvailable, launch=runtime.launch||((exe,args)=>spawn(exe,args,{stdio:"ignore"})), wait=runtime.waitForCdp||waitForCdp, stop=runtime.stopControlled||stopControlled, restore=runtime.restoreStandardApp||restoreStandardApp; let child,value,primaryError,cleanupFailure; await quit(options.app); try { await assertAvailable(port); child=launch(executableFor(options.app),controlledLaunchArgs(port)); if(!child||!Number.isSafeInteger(child.pid)||child.pid<=0)fail("app_launch"); await wait(endpoint,child); value=await work(endpoint,child); } catch(error) { primaryError=error; } finally { if(child){try{await stop(child);}catch(error){cleanupFailure=cleanupFailure??{phase:"controlled_stop",error:machineCode(error.code||error.message)};}}try{await restore(options.app);}catch(error){cleanupFailure=cleanupFailure??{phase:"standard_restore",error:machineCode(error.code||error.message)};} } if(primaryError){if(cleanupFailure)primaryError.controlled_cleanup=cleanupFailure;throw primaryError;}if(cleanupFailure){const error=new Error(cleanupFailure.error);error.code=cleanupFailure.error;error.controlled_cleanup=cleanupFailure;error.cleanup_only=true;throw error;}return value; }
async function run(options) {
  let phase="preflight",started=false,connection,pre,first,post,deltasValue,start,startSettlementValue,startSettlementAtTimeout,startSettlementFinal,responseAfterTimeout=false,stop,stopSettlementValue,readinessValue,sessionReadinessValue,databaseValue,transitionRaw,transitionValue,startNetwork,startNetworkValue,result,db=options.db,cleanupPhase,cleanupFirstBreak,cleanupError;
  try {
    if(!options.app||!options.sha256)fail("installed_hash");
    verifyInstalled(options.app,options.sha256);
    await withControlledApp(options,async(endpoint,child)=>{
      let primaryError;
      try {
        const renderer=await (options.runtime?.waitForRendererReady||waitForRendererReady)(endpoint,options.runtime?.rendererReadyOptions);
        connection=renderer.connection;
        readinessValue=renderer.readiness;
        startNetwork=await connection.enableStartNetworkDiagnostics();
        if(!db){const resolved=await waitForMeetingDb(child.pid,options.databaseRuntime);db=resolved.db;databaseValue=resolved.database;}
        phase="session_readiness";
        let prepared;
        try {
          prepared=await prepareTrustedStart(connection,startNetwork,{
            waitForReadiness:options.runtime?.waitForSessionReadiness||waitForSessionReadiness,
            sessionReadinessOptions:options.sessionReadinessRuntime,
            dispatchInput:options.runtime?.dispatchFormalInput,
            onBeforeInput:()=>{phase="toggle_start";},
          });
        } catch(error) {
          pre=error.pre??pre;
          start=error.input_action??start;
          sessionReadinessValue=error.session_readiness??sessionReadinessValue;
          throw error;
        }
        pre=prepared.pre;
        start=prepared.start;
        sessionReadinessValue=prepared.session_readiness;
        started=true;
        phase="start_settle";
        startSettlementValue=await (options.runtime?.waitForManualStartSettlement||waitForManualStartSettlement)(connection,db,startNetwork,options.startSettlementRuntime);
        transitionRaw=transitionAggregate();
        phase="first_frame";
        const deadline=Date.now()+FRAME_TIMEOUT_MS;
        let frameCandidate;
        while(Date.now()<deadline){const candidate=snapshot(await connection.evaluate(SNAPSHOT)),control=await connection.evaluate(CONTROL),inMeeting=activeMeetings(db);recordTransition(transitionRaw,candidate,inMeeting,control);if(inMeeting===0)fail("first_frame");const decision=firstFrameDecision(pre,candidate);if(decision==="ready"){frameCandidate=candidate;first=frameCandidate;break;}if(decision==="capture_rejected")fail("capture_rejected");await connection.evaluate(RENDER_TICK);}
        if(!first)fail("first_frame");
        phase="window";
        await delay(WINDOW_MS);
        const windowPost=snapshot(await connection.evaluate(SNAPSHOT));
        post=windowPost;
        phase="stop_settle";
        stop=await stopMeeting(connection,db);
        stopSettlementValue={pending_count:0,active_count:activeMeetings(db),stop};
        if(stopSettlementValue.active_count!==0)fail("stop_settlement");
        // A formal stop owns the tail-drain boundary. Count acknowledgements
        // that settle during that boundary instead of freezing them at 25s.
        post=snapshot(await connection.evaluate(SNAPSHOT));
        started=false;
        deltasValue=deltas(first,post);const br=firstBreak(deltasValue);
        if(br)fail(br);
        const aggregates=sqliteCounts(db);
        if(aggregates.outbox!==0||aggregates.foreign_key_violations!==0)fail("sync_integrity");
      } catch(error) {
        primaryError=error;
        if(error.start_settlement_timeout===true)startSettlementAtTimeout=error.start_settlement;
      } finally {
        if(connection&&started){
          cleanupPhase="stop_settle";
          try {
            stopSettlementValue=await (options.runtime?.settleStartedMeeting||settleStartedMeeting)(connection,db,startNetwork,options.stopSettlementRuntime);
            startSettlementFinal=stopSettlementValue.start_settlement??startSettlementFinal;
            stop=stopSettlementValue.stop??stop;
            started=false;
          } catch(error) {
            startSettlementFinal=error.start_settlement_final??error.start_settlement??startSettlementFinal;
            cleanupError=machineCode(error.code||error.message);
            cleanupFirstBreak="stop_settlement";
            stop=error.stop??stop;
            stopSettlementValue=error.stop_settlement??stopSettlementValue;
          }
        }
        startSettlementFinal=startSettlementFinal??startSettlementValue;
        responseAfterTimeout=responseArrivedAfterTimeout(startSettlementAtTimeout,startSettlementFinal);
        if(connection)connection.socket.close();
      }
      if(primaryError){primaryError.acceptance={phase,pre,first,post,deltas:deltasValue,start,start_settlement:primaryError.start_settlement??startSettlementValue,start_settlement_at_timeout:startSettlementAtTimeout,start_settlement_final:startSettlementFinal,response_after_timeout:responseAfterTimeout,stop:primaryError.stop??stop,stop_settlement:primaryError.stop_settlement??stopSettlementValue,readiness:primaryError.readiness??readinessValue,session_readiness:primaryError.session_readiness??sessionReadinessValue,database:primaryError.database??databaseValue,transition:transitionRaw?finishTransition(transitionRaw):transitionValue,start_network:startNetworkReport(startNetwork,transitionRaw),cleanup_phase:cleanupPhase,cleanup_first_break:cleanupFirstBreak,cleanup_error:cleanupError};throw primaryError;}
      if(cleanupError){const error=new Error(cleanupError);error.code=cleanupError;error.cleanup_only=true;error.acceptance={phase,pre,first,post,deltas:deltasValue,start,start_settlement:startSettlementValue,start_settlement_at_timeout:startSettlementAtTimeout,start_settlement_final:startSettlementFinal,response_after_timeout:responseAfterTimeout,stop,stop_settlement:stopSettlementValue,readiness:readinessValue,session_readiness:sessionReadinessValue,database:databaseValue,transition:transitionRaw?finishTransition(transitionRaw):transitionValue,start_network:startNetworkReport(startNetwork,transitionRaw),cleanup_phase:cleanupPhase,cleanup_first_break:cleanupFirstBreak,cleanup_error:cleanupError};throw error;}
    });
    transitionValue=transitionRaw?finishTransition(transitionRaw):transitionValue;
    startNetworkValue=startNetworkReport(startNetwork,transitionValue);
    deltasValue=deltasValue??deltas(first,post);result=report("complete",{pre,first,post,deltas:deltasValue,aggregates:sqliteCounts(db),start,start_settlement:startSettlementValue,start_settlement_at_timeout:startSettlementAtTimeout,start_settlement_final:startSettlementFinal,response_after_timeout:responseAfterTimeout,stop,stop_settlement:stopSettlementValue,readiness:readinessValue,session_readiness:sessionReadinessValue,database:databaseValue,transition:transitionValue,start_network:startNetworkValue,cleanup_phase:cleanupPhase,cleanup_first_break:cleanupFirstBreak,cleanup_error:cleanupError});
  } catch(error) {
    const saved=error.acceptance||{};
    phase=saved.phase??phase;pre=saved.pre??pre;first=saved.first??first;post=saved.post??post;deltasValue=saved.deltas??deltasValue;start=saved.start??start;startSettlementValue=saved.start_settlement??error.start_settlement??startSettlementValue;startSettlementAtTimeout=saved.start_settlement_at_timeout??startSettlementAtTimeout;startSettlementFinal=saved.start_settlement_final??error.start_settlement_final??startSettlementFinal;responseAfterTimeout=saved.response_after_timeout===true||responseArrivedAfterTimeout(startSettlementAtTimeout,startSettlementFinal);stop=saved.stop??error.stop??stop;stopSettlementValue=saved.stop_settlement??error.stop_settlement??stopSettlementValue;readinessValue=saved.readiness??error.readiness??readinessValue;sessionReadinessValue=saved.session_readiness??error.session_readiness??sessionReadinessValue;databaseValue=saved.database??error.database??databaseValue;transitionValue=saved.transition??(transitionRaw?finishTransition(transitionRaw):transitionValue);startNetworkValue=saved.start_network??startNetworkReport(startNetwork,transitionValue);cleanupPhase=saved.cleanup_phase??cleanupPhase;cleanupFirstBreak=saved.cleanup_first_break??cleanupFirstBreak;cleanupError=saved.cleanup_error??cleanupError;
    if(error.controlled_cleanup){cleanupPhase=cleanupPhase??error.controlled_cleanup.phase;cleanupFirstBreak=cleanupFirstBreak??"controlled_lifecycle";cleanupError=cleanupError??error.controlled_cleanup.error;}
    const code=error.cleanup_only?null:machineCode(error.code||error.message);
    const first_break=code===null?null:primaryFirstBreak(code,phase);
    result=report(phase,{pre,first,post,deltas:deltasValue,start,start_settlement:startSettlementValue,start_settlement_at_timeout:startSettlementAtTimeout,start_settlement_final:startSettlementFinal,response_after_timeout:responseAfterTimeout,stop,stop_settlement:stopSettlementValue,readiness:readinessValue,session_readiness:sessionReadinessValue,database:databaseValue,transition:transitionValue,start_network:startNetworkValue,error_code:code,first_break,cleanup_phase:cleanupPhase,cleanup_first_break:cleanupFirstBreak,cleanup_error:cleanupError});
  } finally {
    if(transitionRaw&&!transitionValue)transitionValue=finishTransition(transitionRaw);
    if(result){result.transition=transitionValue??result.transition;result.start_network=startNetworkValue??startNetworkReport(startNetwork,transitionValue);}
    writeReport(result,options.report||REPORT_PATH);
    console.log(JSON.stringify(result));
  }
  return result;
}
async function runInstalled(){ return run(resolveInstalledBundle()); }
if(require.main===module){if(process.argv.length!==2){console.log(JSON.stringify(report("preflight",{error_code:"arguments_forbidden"})));process.exitCode=1;}else runInstalled().then((r)=>{if(r.phase!=="complete")process.exitCode=1});}
module.exports={BACKEND_READY_TIMEOUT_MS,CDP_PORT,COUNTERS,DEFAULT_APP_PATH,DEFAULT_MANIFEST_PATH,DIAGNOSTIC_COUNTERS,FORMAL_CLICK_PROBE_COUNTS,FORMAL_CLICK_PROBE_RESET,FORMAL_CONTROL_RECT,FORMAL_TOGGLE_SELECTOR,SNAPSHOT,STOP_TIMEOUT_MS,activeMeetings,controlledLaunchArgs,deltas,descendantPids,dispatchFormalInput,finishTransition,firstBreak,firstFrameDecision,generationRequestState,hasMeetingsSchema,inputAction,inputPoint,inputProof,inspectOpenMeetingDb,isManualStartRequest,isSessionBootstrapRequest,manualStartGenerationState,markSessionReadinessBoundary,networkErrorClass,openSqliteFiles,physical,prepareTrustedStart,primaryFirstBreak,processTopology,productPages,readiness,recordStartNetworkEvent,recordTransition,releaseManifest,rendererSessionReadinessExpression,report,resolveInstalledBundle,resolveOpenMeetingDb,run,runInstalled,safeResponseStatus,sessionNetworkRecovered,settleStartedMeeting,snapshot,startNetworkAggregate,startNetworkReport,stopComplete,stopPlan,transitionAggregate,waitForManualStartSettlement,waitForMeetingDb,waitForRendererReady,waitForSessionReadiness,withControlledApp,writeReport};
