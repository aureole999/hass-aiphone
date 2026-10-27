// Aiphone Intercom: use HA's authenticated camera WebRTC signaling.
class AiphoneIntercomCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity?.startsWith("camera.")) throw new Error("Select an Aiphone camera entity");
    this.config = config;
    this._statusLookup = this._statusEntity = this._quickReplyEntity = undefined;
    if (!this.shadowRoot) this.attachShadow({mode: "open"});
    this.shadowRoot.innerHTML = `
      <style>
        ha-card{display:block;padding:16px}h3{margin:0 0 12px}video{width:100%;min-height:160px;background:#111;border-radius:8px}
        .buttons{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}button{padding:10px 16px;border-radius:20px;border:0;cursor:pointer;background:var(--primary-color);color:var(--text-primary-color,#fff)}
        button:disabled{opacity:.5;cursor:wait}p{margin-bottom:0;font-size:14px} .end{background:#b3261e}
      </style>
      <ha-card><h3></h3><video autoplay playsinline controls></video>
      <div class="buttons"><button data-action="monitor">Monitor</button><button data-action="talk">Talk / Answer</button>
      <button data-action="reply">Quick reply</button><button data-action="mute" disabled>Mute microphone</button><button class="end" data-action="stop">Hang up / Close</button></div>
      <p role="status" aria-live="polite">Open video to monitor. Tap Talk to enable your microphone.</p></ha-card>`;
    this.shadowRoot.querySelector("h3").textContent = config.title || "Aiphone Doorbell";
    this.shadowRoot.querySelectorAll("button").forEach(button => button.onclick = () => this._action(button.dataset.action));
  }
  set hass(value) {
    this._hass = value;
    if (!this._statusLookup && this.config) {
      this._statusLookup = value.callWS({type:"config/entity_registry/list"}).then(registry => {
        const camera = registry.find(e => e.entity_id === this.config.entity);
        if (camera) {
          const related = registry.filter(e => e.config_entry_id === camera.config_entry_id && e.platform === "aiphone" && !e.disabled_by);
          this._statusEntity = related.find(e => e.unique_id.endsWith("_call_status"))?.entity_id;
          this._quickReplyEntity = this.config.quick_reply_entity || related.find(e => !e.disabled_by && e.unique_id.endsWith("_quick_reply"))?.entity_id;
          this.shadowRoot.querySelector('[data-action="reply"]').hidden = !this._quickReplyEntity;
        }
      }).catch(() => {});
    }
    const state = value.states?.[this._statusEntity]?.state;
    if ((this._pc || this._waitingReply) && state && !["unknown","unavailable"].includes(state)) {
      const labels = {requesting_call:"Requesting call...",connecting_audio:"Connecting audio...",in_call:"In call",audio_interrupted:"Audio interrupted",answered_mic_off:"Answered (microphone off)",preparing_reply:"Preparing reply...",playing_reply:"Playing reply..."};
      this._status(labels[state] || state.charAt(0).toUpperCase() + state.slice(1));
    }
  }
  getCardSize() { return 5; }
  disconnectedCallback() { this._close(); }
  _status(text) { this.shadowRoot.querySelector("p").textContent = text; }
  async _action(action) {
    if (this._busy && action !== "stop") return;
    if (action === "mute") {
      const track = this._mic?.getAudioTracks()[0];
      if (track) {
        track.enabled = !track.enabled;
        this.shadowRoot.querySelector('[data-action="mute"]').textContent = track.enabled ? "Mute microphone" : "Unmute microphone";
      }
      return;
    }
    this._busy = true;
    try {
      if (action === "reply") {
        const registry = await this._hass.callWS({type:"config/entity_registry/list"});
        const camera = registry.find(e => e.entity_id === this.config.entity);
        const entity = this.config.quick_reply_entity || (camera && registry.find(e => e.config_entry_id === camera.config_entry_id && e.platform === "aiphone" && !e.disabled_by && e.unique_id.endsWith("_quick_reply"))?.entity_id);
        if (!entity) throw new Error("Enable Quick Reply in the integration options.");
        await this._hass.callService("switch", "turn_on", {entity_id:entity});
        this._waitingReply = true;
        this._status("Quick reply requested");
      } else if (action === "stop") {
        const entity = await this._hangupEntity();
        await this._close();
        if (entity) await this._hass.callService("button", "press", {entity_id: entity});
        this._status("Closed");
      } else {
        await this._start(action === "talk");
      }
    } catch (error) {
      await this._close();
      this._status(error.message || String(error));
    } finally { this._busy = false; }
  }
  async _hangupEntity() {
    if (this.config.hang_up_entity) return this.config.hang_up_entity;
    const registry = await this._hass.callWS({type:"config/entity_registry/list"});
    const camera = registry.find(e => e.entity_id === this.config.entity);
    return registry.find(e => e.config_entry_id === camera?.config_entry_id && e.platform === "aiphone" && e.unique_id.endsWith("_hang_up"))?.entity_id;
  }
  async _start(talk) {
    if (talk && this._mic) { this._status("Microphone enabled"); return; }
    if (!talk && this._mic) {
      const entity = await this._hangupEntity();
      if (entity) await this._hass.callService("button", "press", {entity_id:entity});
    }
    // Request permission in the click handler before starting any cloud talk.
    const attempt = this._generation || 0;
    let mic;
    if (talk) {
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) throw new Error("Open Home Assistant over HTTPS to use the microphone.");
      mic = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
      if ((this._generation || 0) !== attempt || !this.isConnected) {
        mic.getTracks().forEach(track => track.stop()); return;
      }
    }
    await this._close();
    this._mic = mic;
    const pc = this._pc = new RTCPeerConnection({iceServers:[]});
    const generation = this._generation;
    this._status(talk ? "Requesting call..." : "Connecting video...");
    pc.addTransceiver("video", {direction:"recvonly"});
    if (mic) pc.addTrack(mic.getAudioTracks()[0], mic);
    else pc.addTransceiver("audio", {direction:"recvonly"});
    const remote = new MediaStream();
    const video = this.shadowRoot.querySelector("video");
    video.srcObject = remote;
    video.muted = false;
    pc.ontrack = event => { remote.addTrack(event.track); video.play().catch(() => this._status("Connected. Tap the video play button to enable sound.")); };
    pc.onconnectionstatechange = () => {
      if (this._pc !== pc) return;
      if (pc.connectionState === "connected") this._status(talk ? "Audio connected" : "Monitoring");
      if (["failed","closed"].includes(pc.connectionState)) { this._status("Connection ended. Reopen video."); this._close(); }
    };
    await pc.setLocalDescription(await pc.createOffer());
    await this._gather(pc);
    if (this._pc !== pc || !this.isConnected) { await this._close(); return; }
    const unsubscribe = await this._hass.connection.subscribeMessage(event => {
      if (this._pc !== pc) return;
      if (event.type === "answer") pc.setRemoteDescription({type:"answer",sdp:event.answer}).catch(e => this._fail(e));
      if (event.type === "candidate") pc.addIceCandidate(event.candidate).catch(e => this._fail(e));
      if (event.type === "error") this._fail(new Error(event.message || "Could not start the call. Check Home Assistant logs."));
    }, {type:"camera/webrtc/offer", entity_id:this.config.entity, offer:pc.localDescription.sdp});
    if (this._pc !== pc || this._generation !== generation) unsubscribe();
    else this._unsubscribe = unsubscribe;
    this.shadowRoot.querySelector('[data-action="mute"]').disabled = !mic;
  }
  async _gather(pc) {
    if (pc.iceGatheringState === "complete") return;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { cleanup(); reject(new Error("ICE gathering timed out. Check the connection.")); }, 10000);
      const changed = () => { if (pc.iceGatheringState === "complete" || pc.signalingState === "closed") { cleanup(); resolve(); } };
      const cleanup = () => { clearTimeout(timer); pc.removeEventListener("icegatheringstatechange", changed); pc.removeEventListener("signalingstatechange", changed); };
      pc.addEventListener("icegatheringstatechange", changed);
      pc.addEventListener("signalingstatechange", changed);
    });
  }
  _fail(error) { this._status(error.message); this._close(); }
  async _close() {
    this._generation = (this._generation || 0) + 1;
    const pc = this._pc;
    this._pc = null;
    this._mic?.getTracks().forEach(track => track.stop());
    this._mic = null;
    if (this._unsubscribe) { const unsubscribe = this._unsubscribe; this._unsubscribe = null; await unsubscribe().catch(() => {}); }
    if (pc) { pc.onconnectionstatechange = null; pc.close(); }
    if (this.shadowRoot) {
      this.shadowRoot.querySelector("video").srcObject = null;
      this.shadowRoot.querySelector('[data-action="mute"]').disabled = true;
      this.shadowRoot.querySelector('[data-action="mute"]').textContent = "Mute microphone";
    }
  }
}
if (!customElements.get("aiphone-intercom-card")) customElements.define("aiphone-intercom-card", AiphoneIntercomCard);
window.customCards = window.customCards || [];
window.customCards.push({type:"aiphone-intercom-card",name:"Aiphone Intercom",description:"Doorbell video, quick replies and two-way audio"});
