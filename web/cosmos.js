/* COSMOS: dependency-free, local-first WebGL planet scene.
 * Profile buttons create ORBIT sessions; imagery is decorative and never performs API actions.
 * 4K user assets are optional: procedural textures are used until installed.
 */
(() => {
  'use strict';
  const ROOT = '/assets/cosmos/';
  const TEXTURES = ['planet_violet_texture.png','planet_blue_texture.png','planet_amber_texture.png','planet_moon_texture.png'];
  const COLORS = [[105,48,166],[33,104,156],[173,103,52],[122,131,151]];
  const BODIES = [
    {x:-0.60,y:-0.08,z:0.22,r:0.77,tilt:0.16,speed:0.12},
    {x:1.05,y:0.57,z:-0.22,r:0.37,tilt:0.22,speed:-0.23},
    {x:1.31,y:-0.62,z:-0.35,r:0.31,tilt:-0.12,speed:0.21},
    {x:-1.49,y:0.68,z:-0.40,r:0.24,tilt:0.10,speed:-0.08}
  ];
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let scene = null, frame = 0, profile = 'ALEX', controller = null;

  function shader(gl, type, source) {
    const item = gl.createShader(type);
    gl.shaderSource(item, source); gl.compileShader(item);
    if (!gl.getShaderParameter(item, gl.COMPILE_STATUS)) {
      const message = gl.getShaderInfoLog(item); gl.deleteShader(item); throw new Error(message);
    }
    return item;
  }

  function program(gl) {
    const vertex = shader(gl, gl.VERTEX_SHADER, [
      'attribute vec3 aPoint; attribute vec2 aUV;',
      'uniform vec3 uOrigin; uniform float uRadius, uAngle, uTilt, uAspect;',
      'varying vec2 vUV; varying vec3 vNormal;',
      'void main(){',
      'float c=cos(uAngle),s=sin(uAngle),t=cos(uTilt),q=sin(uTilt);',
      'vec3 r=vec3(c*aPoint.x+s*aPoint.z,aPoint.y,-s*aPoint.x+c*aPoint.z);',
      'vec3 n=vec3(r.x,t*r.y-q*r.z,q*r.y+t*r.z);',
      'vec3 world=n*uRadius+uOrigin;',
      'float zoom=3.8/(4.4-world.z);',
      'gl_Position=vec4(world.x*zoom/uAspect,world.y*zoom,(world.z+2.0)/12.0,1.0);',
      'vUV=aUV; vNormal=n;',
      '}'
    ].join('\n'));
    const fragment = shader(gl, gl.FRAGMENT_SHADER, [
      'precision mediump float;',
      'uniform sampler2D uTexture; uniform float uGlow;',
      'varying vec2 vUV; varying vec3 vNormal;',
      'void main(){',
      'vec3 n=normalize(vNormal);',
      'float light=max(dot(n,normalize(vec3(-0.44,0.63,0.68))),0.0);',
      'float rim=pow(1.0-max(n.z,0.0),3.0);',
      'vec3 tex=texture2D(uTexture,vUV).rgb;',
      'vec3 color=tex*(0.17+0.82*light)*uGlow + vec3(0.12,0.20,0.36)*rim*0.33;',
      'gl_FragColor=vec4(color,1.0);',
      '}'
    ].join('\n'));
    const p = gl.createProgram();
    gl.attachShader(p,vertex);gl.attachShader(p,fragment);gl.linkProgram(p);
    gl.deleteShader(vertex);gl.deleteShader(fragment);
    if (!gl.getProgramParameter(p,gl.LINK_STATUS)) {
      const message=gl.getProgramInfoLog(p);gl.deleteProgram(p);throw new Error(message);
    }
    return p;
  }

  function geometry(latitudes=36,longitudes=56) {
    const points=[], indices=[];
    for(let lat=0;lat<=latitudes;lat++){
      const theta=lat*Math.PI/latitudes;
      for(let lon=0;lon<=longitudes;lon++){
        const phi=lon*Math.PI*2/longitudes;
        points.push(Math.sin(theta)*Math.cos(phi),Math.cos(theta),Math.sin(theta)*Math.sin(phi),lon/longitudes,lat/latitudes);
      }
    }
    for(let lat=0;lat<latitudes;lat++)for(let lon=0;lon<longitudes;lon++){
      const a=lat*(longitudes+1)+lon,b=a+longitudes+1;
      indices.push(a,b,a+1,b,b+1,a+1);
    }
    return {vertices:new Float32Array(points),indices:new Uint16Array(indices)};
  }

  function fallbackTexture(gl, index) {
    const cvs=document.createElement('canvas');cvs.width=512;cvs.height=256;
    const ctx=cvs.getContext('2d');const img=ctx.createImageData(cvs.width,cvs.height);
    const color=COLORS[index];
    for(let y=0;y<cvs.height;y++)for(let x=0;x<cvs.width;x++){
      const sx=x/cvs.width, sy=y/cvs.height, pole=Math.sin(sy*Math.PI);
      const pattern=Math.sin(sy*61+Math.sin(sx*19)*2.1)*0.15
       +Math.cos(sx*39+sy*27)*0.11+Math.sin(sx*107+sy*119)*0.055;
      const light=0.67+pattern*pole;
      const k=(y*cvs.width+x)*4;
      for(let c=0;c<3;c++)img.data[k+c]=Math.max(0,Math.min(255,color[c]*light+(index===3?24:0)));
      img.data[k+3]=255;
    }
    ctx.putImageData(img,0,0);
    const texture=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,texture);
    gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,cvs);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
    return texture;
  }

  function createRenderer(canvas) {
    const gl=canvas.getContext('webgl',{alpha:true,antialias:true,premultipliedAlpha:false});
    if (!gl) return null;
    const p=program(gl), geo=geometry(), vbo=gl.createBuffer(), ibo=gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER,vbo);gl.bufferData(gl.ARRAY_BUFFER,geo.vertices,gl.STATIC_DRAW);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,ibo);gl.bufferData(gl.ELEMENT_ARRAY_BUFFER,geo.indices,gl.STATIC_DRAW);
    gl.useProgram(p);
    const attributes={point:gl.getAttribLocation(p,'aPoint'),uv:gl.getAttribLocation(p,'aUV')};
    gl.enableVertexAttribArray(attributes.point);gl.vertexAttribPointer(attributes.point,3,gl.FLOAT,false,20,0);
    gl.enableVertexAttribArray(attributes.uv);gl.vertexAttribPointer(attributes.uv,2,gl.FLOAT,false,20,12);
    const uniforms={origin:gl.getUniformLocation(p,'uOrigin'),radius:gl.getUniformLocation(p,'uRadius'),
      angle:gl.getUniformLocation(p,'uAngle'),tilt:gl.getUniformLocation(p,'uTilt'),
      aspect:gl.getUniformLocation(p,'uAspect'),glow:gl.getUniformLocation(p,'uGlow')};
    gl.uniform1i(gl.getUniformLocation(p,'uTexture'),0);
    const textures=TEXTURES.map((file,i)=>fallbackTexture(gl,i));
    let alive=true;
    TEXTURES.forEach((file,i)=>{
      const img=new Image();
      img.onload=()=>{
        if(!alive)return;
        try {
          // Upload a small GPU texture even when the source is 4096x2048.
          const buffer=document.createElement('canvas');buffer.width=1024;buffer.height=512;
          buffer.getContext('2d').drawImage(img,0,0,buffer.width,buffer.height);
          gl.bindTexture(gl.TEXTURE_2D,textures[i]);
          gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,buffer);
        }catch(error){console.warn('COSMOS texture fallback:',file,error);}
      };
      img.onerror=()=>{}; // Asset pack is optional; generated texture stays visible.
      img.src=ROOT+file;
    });
    gl.enable(gl.DEPTH_TEST);gl.depthFunc(gl.LEQUAL);gl.disable(gl.CULL_FACE);
    return {
      draw(time) {
        const rect=canvas.getBoundingClientRect();
        if(!rect.width||!rect.height)return;
        const ratio=Math.min(window.devicePixelRatio||1,1.6);
        const w=Math.min(1400,Math.round(rect.width*ratio));
        const h=Math.min(700,Math.round(rect.height*ratio));
        if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;gl.viewport(0,0,w,h);}
        gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
        gl.useProgram(p);gl.bindBuffer(gl.ARRAY_BUFFER,vbo);gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,ibo);
        gl.vertexAttribPointer(attributes.point,3,gl.FLOAT,false,20,0);
        gl.vertexAttribPointer(attributes.uv,2,gl.FLOAT,false,20,12);
        gl.uniform1f(uniforms.aspect,w/h);
        BODIES.forEach((body,i)=>{
          gl.activeTexture(gl.TEXTURE0);gl.bindTexture(gl.TEXTURE_2D,textures[i]);
          gl.uniform3f(uniforms.origin,body.x,body.y,body.z);
          gl.uniform1f(uniforms.radius,body.r);
          gl.uniform1f(uniforms.tilt,body.tilt);
          gl.uniform1f(uniforms.angle,time*body.speed);
          gl.uniform1f(uniforms.glow,i===0&&profile==='ALEX'?1.45:i===1&&profile==='NOVA'?1.65:1.0);
          gl.drawElements(gl.TRIANGLES,geo.indices.length,gl.UNSIGNED_SHORT,0);
        });
      },
      destroy() {
        alive=false;
        textures.forEach(t=>gl.deleteTexture(t));
        gl.deleteBuffer(vbo);gl.deleteBuffer(ibo);gl.deleteProgram(p);
      }
    };
  }

  function stop(){
    if(frame)cancelAnimationFrame(frame);
    frame=0;
    if(controller)controller.destroy();
    controller=null;scene=null;
  }

  function setProfile(next){
    profile=next==='NOVA'?'NOVA':next==='HERMES'?'HERMES':'ALEX';
    if(scene){
      scene.dataset.profile=profile;
      scene.querySelectorAll('[data-cosmos-agent]').forEach(b=>{
        b.setAttribute('aria-pressed',String(b.dataset.cosmosAgent===profile));
      });
      if(controller&&reducedMotion.matches)controller.draw(0);
    }
  }

  function mount(intro,onSelect){
    if(!intro)return;
    stop();
    const stage=document.createElement('div');stage.className='cosmos-scene';
    stage.dataset.profile=profile;
    stage.setAttribute('aria-label','COSMOS — 3D планетна система на агентите');
    const canvas=document.createElement('canvas');canvas.className='cosmos-canvas';
    canvas.setAttribute('aria-hidden','true');
    const fallback=document.createElement('div');fallback.className='cosmos-fallback';
    fallback.setAttribute('aria-hidden','true');
    for(let i=0;i<4;i++){
      const orb=document.createElement('span');orb.className='cosmos-fallback-orb orb-'+i;fallback.append(orb);
    }
    const nav=document.createElement('div');nav.className='cosmos-nav';
    for(const name of ['ALEX','NOVA','HERMES']){
      const button=document.createElement('button');button.type='button';
      button.className='cosmos-agent';button.dataset.cosmosAgent=name;
      button.textContent=name;button.setAttribute('aria-pressed',String(name===profile));
      button.addEventListener('click',()=>onSelect(name));nav.append(button);
    }
    const caption=document.createElement('span');caption.className='cosmos-caption';
    caption.textContent='COSMOS / EXPLORE AGENT NETWORK';
    stage.append(canvas,fallback,nav,caption);intro.append(stage);scene=stage;
    const errorHandler=e=>{e.preventDefault();stop();stage.classList.add('webgl-unavailable');};
    canvas.addEventListener('webglcontextlost',errorHandler,{once:true});
    try{controller=createRenderer(canvas);}catch(error){
      console.warn('COSMOS WebGL unavailable:',error);
      controller=null;
    }
    if(!controller){stage.classList.add('webgl-unavailable');return;}
    function tick(now){
      if(scene!==stage||!stage.isConnected){stop();return;}
      if(!document.hidden)controller.draw(reducedMotion.matches?0:now/1000);
      if(!reducedMotion.matches)frame=requestAnimationFrame(tick);
    }
    frame=requestAnimationFrame(tick);
    const onMotion=()=>{if(scene!==stage)return;if(frame)cancelAnimationFrame(frame);frame=0;frame=requestAnimationFrame(tick);};
    reducedMotion.addEventListener?.('change',onMotion,{once:true});
  }
  window.ORBIT_COSMOS={mount,setProfile};
})();
