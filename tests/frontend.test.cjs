const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function fixture({denied = false, connected = true} = {}) {
  let Card, offers = [], micRequests = 0, unsubscribed = 0, services = [];
  const audioTrack = {enabled:true, stopped:false, stop(){this.stopped=true;}};
  const mic = {getTracks:()=>[audioTrack],getAudioTracks:()=>[audioTrack]};
  const controls = new Map();
  const control = key => {
    if (!controls.has(key)) controls.set(key,{textContent:'',disabled:false,srcObject:null,play:async()=>{}});
    return controls.get(key);
  };
  class Peer {
    constructor(){this.directions=[];this.iceGatheringState='complete';this.connectionState='new';}
    addTransceiver(kind, options){this.directions.push([kind,options.direction]);}
    addTrack(){this.directions.push(['audio','sendrecv']);}
    async createOffer(){return {type:'offer',sdp:'mock-offer'};}
    async setLocalDescription(value){this.localDescription=value;}
    async setRemoteDescription(value){this.remoteDescription=value;}
    close(){this.connectionState='closed';}
  }
  const context = {
    HTMLElement:class {},
    customElements:{get:()=>undefined,define:(_,ctor)=>Card=ctor},
    window:{isSecureContext:true},
    navigator:{mediaDevices:{getUserMedia:async()=>{micRequests++;if(denied)throw new Error('Permission denied');return mic;}}},
    RTCPeerConnection:Peer,
    MediaStream:class {addTrack(){}},setTimeout,clearTimeout,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../custom_components/aiphone/frontend/aiphone-intercom-card.js'),'utf8'), context);
  const card = new Card();
  card.config={entity:'camera.entrance'};
  card.shadowRoot={querySelector:control};
  card.isConnected=connected;
  card.hass={
    callWS:async()=>[
      {entity_id:'camera.entrance',config_entry_id:'entry',platform:'aiphone',unique_id:'camera'},
      {entity_id:'button.end',config_entry_id:'entry',platform:'aiphone',unique_id:'id_hang_up'},
    ],
    callService:async(...args)=>services.push(args),
    connection:{subscribeMessage:async(callback,message)=>{
      offers.push(message); context.lastEvent=callback;
      return async()=>unsubscribed++;
    }},
  };
  return {card,context,audioTrack,controls,offers,services,get micRequests(){return micRequests;},get unsubscribed(){return unsubscribed;}};
}

test('Monitor uses authenticated HA offer without requesting a microphone', async()=>{
  const f=fixture(); await f.card._action('monitor');
  assert.equal(f.micRequests,0);
  assert.deepEqual(JSON.parse(JSON.stringify(f.offers[0])),{type:'camera/webrtc/offer',entity_id:'camera.entrance',offer:'mock-offer'});
  assert.equal(f.card._pc.directions[1][1],'recvonly');
  await f.card._close(); assert.equal(f.unsubscribed,1);
});

test('Talk captures only after explicit action; mute and hangup stop microphone', async()=>{
  const f=fixture(); await f.card._action('talk');
  assert.equal(f.micRequests,1);
  assert.equal(f.card._pc.directions[1][1],'sendrecv');
  await f.card._action('mute'); assert.equal(f.audioTrack.enabled,false);
  await f.card._action('mute'); assert.equal(f.audioTrack.enabled,true);
  await f.card._action('stop');
  assert.equal(f.audioTrack.stopped,true); assert.equal(f.unsubscribed,1);
  assert.equal(f.services[0][2].entity_id,'button.end');
});

test('Denied microphone permission sends no offer or cloud action', async()=>{
  const f=fixture({denied:true}); await f.card._action('talk');
  assert.equal(f.offers.length,0); assert.equal(f.services.length,0);
  assert.match(f.controls.get('p').textContent,/Permission denied/);
});

test('Detached card cannot leave a captured microphone or subscription running', async()=>{
  const f=fixture({connected:false}); await f.card._action('talk');
  assert.equal(f.audioTrack.stopped,true); assert.equal(f.offers.length,0);
});

test('Backend rejection stops microphone and surfaces its message', async()=>{
  const f=fixture(); await f.card._action('talk');
  f.context.lastEvent({type:'error',message:'Call rejected'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(f.audioTrack.stopped,true); assert.equal(f.unsubscribed,1);
  assert.equal(f.controls.get('p').textContent,'Call rejected');
});

test('Hangup during permission prompt prevents late capture from reopening a call', async()=>{
  const f=fixture(); let grant;
  f.context.navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>grant=resolve);
  const starting=f.card._action('talk');
  await f.card._action('stop');
  grant({getTracks:()=>[f.audioTrack],getAudioTracks:()=>[f.audioTrack]});
  await starting;
  assert.equal(f.audioTrack.stopped,true); assert.equal(f.offers.length,0);
});
