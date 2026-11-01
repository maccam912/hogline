const $=s=>document.querySelector(s);
const game=$('#app').dataset.game,api=`/api/games/${encodeURIComponent(game)}/reconstructions`;
const colors=['#3bdec0','#ff9b83','#83baff','#d9a1ff','#ffe28d','#a1d87b'];
const canvas=$('#sheet'),ctx=canvas.getContext('2d'),video=$('#source');
let data=null,enabled=new Set(),selected=null,t=0,playing=false,last=0,maxTime=45,markers=[],warp=null;
let opening=null,needsClip=null,lastWarp=0,videoRow=null;
$('#back').href=`/api/games/${encodeURIComponent(game)}/skill-estimates/report`;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const asset=(row,name)=>`${api}/${encodeURIComponent(row.key)}/${name}`;
function status(message,error=false){$('#status').textContent=message;$('#status').setAttribute('role',error?'alert':'status')}
async function request(url,options){const r=await fetch(url,options);if(!r.ok){let d=await r.json().catch(()=>({}));throw Error(d.detail||`Request failed (${r.status})`)}return r.json()}
function interpolate(grid,time){if(!grid?.length)return null;if(time<grid[0][0])return null;
 let i=grid.findIndex(p=>p[0]>=time);if(i<0)return grid.at(-1).slice(1);if(i===0)return grid[0].slice(1);
 const a=grid[i-1],b=grid[i],f=(time-a[0])/(b[0]-a[0]);return[a[1]+f*(b[1]-a[1]),a[2]+f*(b[2]-a[2])]}
function position(x,s,target=canvas){const D=34.7472,scale=(target.width-75)/(D+8);return[38+(s+4)*scale,target.height/2+x*scale]}
function color(row){return colors[Math.max(0,data.players.findIndex(p=>p.person_id===row.person_id))%colors.length]}
function ice(c=ctx,target=canvas){c.fillStyle='#183441';c.fillRect(0,0,target.width,target.height);
 const scale=(target.width-75)/(34.7472+8),y=target.height/2;
 c.fillStyle='#edf3f3';c.fillRect(24,y-2.375*scale,target.width-48,4.75*scale);
 for(const s of[0,34.7472]){const[x]=position(0,s,target);for(const[r,color]of[[1.8288,'#68a9ca'],[1.2192,'#edf3f3'],[.6096,'#abc888'],[.1524,'#edf3f3']]){c.beginPath();c.arc(x,y,r*scale,0,7);c.fillStyle=color;c.fill()}}
 c.strokeStyle='#b4c5c9';c.lineWidth=1;c.beginPath();c.moveTo(24,y);c.lineTo(target.width-24,y);c.stroke();
 for(const s of[6.4008,34.7472-6.4008]){const[x]=position(0,s,target);c.strokeStyle='#cc6874';c.beginPath();c.moveTo(x,y-2.375*scale);c.lineTo(x,y+2.375*scale);c.stroke()}}
function drawSingle(){if(!videoRow)return;const target=$('#single-sheet'),c=target.getContext('2d');ice(c,target);
 const tr=videoRow.trajectory,g=tr.grid;for(let i=1;i<g.length;i++){const a=g[i-1],b=g[i],past=b[0]<=t;
 c.setLineDash(tr.observation_times.some(v=>Math.abs(v-(a[0]+b[0])/2)<.35)?[]:[6,5]);
 c.strokeStyle=past?color(videoRow):'#a6b6bf';c.lineWidth=past?3:1;c.beginPath();c.moveTo(...position(a[1],a[2],target));c.lineTo(...position(b[1],b[2],target));c.stroke()}
 c.setLineDash([]);const rest=['observed_rest','apparent_rest'].includes(tr.status),age=t-g.at(-1)[0];if(!rest&&age>2)return;const p=interpolate(g,t);if(p){const[x,y]=position(...p,target);c.globalAlpha=rest||age<=0?1:Math.max(0,1-age/2);c.beginPath();c.arc(x,y,7,0,7);c.fillStyle=rest?color(videoRow):'#edf3f3';c.fill();c.strokeStyle=color(videoRow);c.lineWidth=2;c.stroke();c.globalAlpha=1}}
function draw(){ice();markers=[];if(!data)return;
 for(const row of data.throws){if(!enabled.has(row.person_id)||row.state!=='ready')continue;
 const tr=row.trajectory,g=tr.grid,c=color(row),end=g.at(-1)[0];
 if($('#trails').checked){for(let i=1;i<g.length&&g[i][0]<=t;i++){const a=g[i-1],b=g[i],mid=(a[0]+b[0])/2;
 const support=tr.observation_times.some(v=>Math.abs(v-mid)<.35);ctx.setLineDash(support?[]:[6,5]);
 ctx.globalAlpha=(selected===row.candidate_id ? .95 : .32);ctx.strokeStyle=c;ctx.lineWidth=selected===row.candidate_id?4:2;
 ctx.beginPath();ctx.moveTo(...position(a[1],a[2]));ctx.lineTo(...position(b[1],b[2]));ctx.stroke()}}
 ctx.setLineDash([]);let alpha=1;
 if(t>end&&!['observed_rest','apparent_rest'].includes(tr.status))alpha=Math.max(0,1-(t-end)/2);
 const p=interpolate(g,t);if(!p||alpha<=0)continue;const[x,y]=position(...p);ctx.globalAlpha=alpha;
 ctx.beginPath();ctx.arc(x,y,selected===row.candidate_id?9:6,0,7);ctx.fillStyle=['observed_rest','apparent_rest'].includes(tr.status)||t<=end?c:'#edf3f3';ctx.fill();
 ctx.strokeStyle='#183441';ctx.lineWidth=1.5;ctx.stroke();ctx.fillStyle='#19303b';ctx.font='bold 10px system-ui';ctx.textAlign='center';ctx.fillText(row.shot_number,x,y+3);
 markers.push({x,y,row})}
 ctx.globalAlpha=1;$('#time').textContent=`${t.toFixed(2)} s`;$('#scrub').value=t;drawSingle();
}
function detail(row){selected=row.candidate_id;$('#detail').innerHTML=`<strong style="color:${color(row)}">${esc(row.player)} · throw ${row.shot_number}</strong> · End ${row.end??'?'}
 <p>${esc(row.trajectory?.status?.replaceAll('_',' ')||row.state)} · ${row.trajectory?.observations??0} camera positions.
 Longest gap: ${(row.trajectory?.longest_gap_seconds??0).toFixed(1)} s.</p>
 <button id="open-selected">Open this throw’s reconstruction</button>`;
 $('#open-selected').onclick=()=>openThrow(row.candidate_id);draw()}
async function generate(ids,includeClip=false){status(includeClip?'Generating the throw and its original clip…':'Generating selected throw paths locally…');
 const body={include_clip:includeClip,retry_failed:true};if(ids)body.candidate_ids=ids;
 try{await request(api,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});await load()}
 catch(e){status(e.message,true)}}
async function load(){try{const next=await request(api);const initial=!data;data=next;if(initial)enabled=new Set(next.players.map(p=>p.person_id));
 const ready=data.throws.filter(r=>r.state==='ready').length;$('#coverage').textContent=`${ready} / ${data.throws.length} throw paths ready`;
 $('#generate').disabled=data.stale||data.throws.every(r=>r.state==='ready'||r.state==='queued'||r.state==='running');
 $('#players').innerHTML=data.players.map((p,i)=>`<label style="color:${colors[i%colors.length]}"><input type="checkbox" value="${esc(p.person_id)}" ${enabled.has(p.person_id)?'checked':''}>${esc(p.name)}</label>`).join('');
 $('#players').querySelectorAll('input').forEach(input=>input.onchange=()=>{input.checked?enabled.add(input.value):enabled.delete(input.value);draw()});
 maxTime=Math.max(10,...data.throws.filter(r=>r.trajectory).map(r=>r.trajectory.duration_seconds+2));$('#scrub').max=maxTime;
 $('#throws').innerHTML=data.throws.map(r=>`<div class="throw"><strong style="color:${color(r)}">${esc(r.player)} · ${r.shot_number}</strong>
 <p>End ${r.end??'?'} · ${esc(r.state==='ready'?r.trajectory.status.replaceAll('_',' '):r.state)}${r.error?` · ${esc(r.error)}`:''}</p>
 <button data-open="${esc(r.candidate_id)}">${r.state==='ready'?'View reconstruction':'Generate reconstruction'}</button></div>`).join('');
 $('#throws').querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>openThrow(b.dataset.open));
 const active=data.throws.filter(r=>['queued','running'].includes(r.state)||['queued','running'].includes(r.clip_job?.state)).length;
 if(active)status(`${active} reconstruction request${active===1?'':'s'} queued or running. Results appear here as they finish.`);
 else if(ready)status(`${ready} paths ready. Use the player filters to compare one player’s throws or the whole team.`);
 if(data.stale)status('Review evidence changed; refresh the player report before generating reconstructions.',true);
 if(needsClip){const row=data.throws.find(r=>r.candidate_id===needsClip);if(row?.result?.artifacts?.['source-shot.mp4']){needsClip=null;await attachVideo(row)}else if(row?.clip_job?.state==='failed'){status(row.clip_job.error||'Clip generation failed',true);needsClip=null}}
 draw();if(initial){const cid=new URLSearchParams(location.search).get('throw');if(cid)await openThrow(cid)}
 }catch(e){status(e.message,true)}}
async function openThrow(cid){const row=data.throws.find(r=>r.candidate_id===cid);if(!row)return;
 video.pause();
 detail(row);$('#single').hidden=false;$('#single-title').textContent=`${row.player} · throw ${row.shot_number}`;
 if(!row.result?.artifacts?.['source-shot.mp4']){needsClip=cid;$('#single-status').textContent='Generating original clip and reconstruction. You can keep using the team replay.';await generate([cid],true);return}
 await attachVideo(row)}
async function attachVideo(row){if(opening===row.key&&video.src)return;selected=row.candidate_id;
 const geometry=await request(asset(row,'geometry.json'));opening=row.key;videoRow=row;video.src=asset(row,'source-shot.mp4');$('#download').href=video.src;
 $('#single-status').textContent=`${row.trajectory.status.replaceAll('_',' ')}. The source video and top-down sheet share the same clock.`;
 warp?.dispose();warp=createWarp($('#warp'),geometry);video.ontimeupdate=()=>{t=Math.max(0,video.currentTime+row.result.clip_start_seconds-row.trajectory.alignment_source_seconds);playing=false;$('#play').textContent='Play all';draw();warp?.draw(video)};
 video.onloadeddata=()=>warp?.draw(video);$('#sync').onclick=()=>{video.currentTime=Math.max(0,row.trajectory.alignment_source_seconds-row.result.clip_start_seconds);video.play()};
 $('#single').scrollIntoView({behavior:'smooth',block:'start'})}
$('#generate').onclick=()=>generate(null);$('#play').onclick=()=>{playing=!playing;video.pause();$('#play').textContent=playing?'Pause':'Play all'};
$('#reset').onclick=()=>{t=0;draw()};$('#scrub').oninput=e=>{playing=false;$('#play').textContent='Play all';t=Number(e.target.value);draw()};
$('#trails').onchange=draw;canvas.onclick=e=>{const r=canvas.getBoundingClientRect(),x=(e.clientX-r.left)*canvas.width/r.width,y=(e.clientY-r.top)*canvas.height/r.height;
 const nearest=markers.map(m=>({...m,d:Math.hypot(m.x-x,m.y-y)})).sort((a,b)=>a.d-b.d)[0];if(nearest?.d<25)detail(nearest.row)};
function animate(now){if(!video.paused&&video.readyState>=2&&videoRow){t=Math.max(0,video.currentTime+videoRow.result.clip_start_seconds-videoRow.trajectory.alignment_source_seconds);draw();if(now-lastWarp>65){warp?.draw(video);lastWarp=now}}if(playing){t+=(now-(last||now))/1000*Number($('#speed').value);if(t>maxTime){if($('#loop').checked)t=0;else{t=maxTime;playing=false;$('#play').textContent='Play all'}}draw()}last=now;requestAnimationFrame(animate)}
requestAnimationFrame(animate);load();setInterval(load,5000);

// A single shader reprojects the original video onto the common ice plane.
function createWarp(c,geometry){const gl=c.getContext('webgl',{alpha:false});if(!gl){$('#single-status').textContent+=' Video warping is unavailable in this browser; the 2D path remains usable.';return null}
 const vertex=`attribute vec2 pos;varying vec2 uv;void main(){uv=pos*.5+.5;gl_Position=vec4(pos,0.,1.);}`;
 const fragment=`precision highp float;varying vec2 uv;uniform sampler2D image;uniform mat3 maps[4];uniform vec4 bounds[4];uniform vec2 flags[4];uniform float distance;
 void main(){vec2 w=vec2((uv.x-.5)*11.,uv.y*(distance+8.)-4.);vec3 sum=vec3(0.);float total=0.;
 for(int i=0;i<4;i++){vec3 q=maps[i]*vec3(w,1.);vec2 p=q.xy/q.z;vec4 b=bounds[i];vec2 size=b.zw-b.xy;
 float valid=step(0.00001,q.z)*step(12.,p.x)*step(12.,p.y)*step(p.x,size.x-12.)*step(p.y,size.y-12.)*step(abs(w.x),2.375);
 float depth=mix(distance-w.y,w.y,flags[i].y);float weight=flags[i].x>.5?clamp((22.-depth)/8.,0.,1.):8.*clamp((2.1-abs(depth))/.3,0.,1.);
 if(flags[i].x>.5)valid*=step(p.y,660.);vec2 tex=(p+b.xy)/vec2(1280.,720.);
 sum+=texture2D(image,vec2(tex.x,1.-tex.y)).rgb*valid*weight;total+=valid*weight;}
 gl_FragColor=vec4(total>.001?sum/total:vec3(.09,.19,.24),1.);}`;
 function shader(type,source){const s=gl.createShader(type);gl.shaderSource(s,source);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));return s}
 try{const program=gl.createProgram(),vs=shader(gl.VERTEX_SHADER,vertex),fs=shader(gl.FRAGMENT_SHADER,fragment);gl.attachShader(program,vs);gl.attachShader(program,fs);gl.linkProgram(program);gl.deleteShader(vs);gl.deleteShader(fs);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(program));gl.useProgram(program);
 const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,1,1]),gl.STATIC_DRAW);
 const p=gl.getAttribLocation(program,'pos');gl.enableVertexAttribArray(p);gl.vertexAttribPointer(p,2,gl.FLOAT,false,0,0);
 const matrices=geometry.cameras.flatMap(c=>[c.matrix[0][0],c.matrix[1][0],c.matrix[2][0],c.matrix[0][1],c.matrix[1][1],c.matrix[2][1],c.matrix[0][2],c.matrix[1][2],c.matrix[2][2]]);
 gl.uniformMatrix3fv(gl.getUniformLocation(program,'maps[0]'),false,new Float32Array(matrices));
 gl.uniform4fv(gl.getUniformLocation(program,'bounds[0]'),new Float32Array(geometry.cameras.flatMap(c=>c.bounds)));
 gl.uniform2fv(gl.getUniformLocation(program,'flags[0]'),new Float32Array(geometry.cameras.flatMap(c=>[Number(c.perspective),Number(c.own)])));
 gl.uniform1f(gl.getUniformLocation(program,'distance'),geometry.tee_distance_m);
 const texture=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,texture);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL,true);
 return{dispose(){gl.deleteTexture(texture);gl.deleteBuffer(buffer);gl.deleteProgram(program)},draw(v){if(v.readyState<2)return;gl.viewport(0,0,c.width,c.height);gl.bindTexture(gl.TEXTURE_2D,texture);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGB,gl.RGB,gl.UNSIGNED_BYTE,v);gl.drawArrays(gl.TRIANGLE_STRIP,0,4)}}
 }catch(e){$('#single-status').textContent+=` Video warp unavailable: ${e.message}`;return null}}
