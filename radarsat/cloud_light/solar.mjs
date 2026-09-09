// Solar position: NOAA's fractional-year approximation, evaluated at image UTC.
// https://gml.noaa.gov/grad/solcalc/solareqns.PDF
export const radians = Math.PI / 180;
export const clamp = (x, lo = 0, hi = 1) => Math.min(hi, Math.max(lo, x));
export function smoothstep(lo, hi, x) {
  const t = clamp((x - lo) / (hi - lo));
  return t * t * (3 - 2 * t);
}
export function solarTerms(time) {
  const d = new Date(time);
  if (!Number.isFinite(d.getTime())) throw new Error('Invalid image timestamp');
  const year = d.getUTCFullYear();
  const yearStart = Date.UTC(year, 0, 1);
  const days = (Date.UTC(year + 1, 0, 1) - yearStart) / 86400000;
  const day = Math.floor((d.getTime() - yearStart) / 86400000) + 1;
  const minutes = d.getUTCHours() * 60 + d.getUTCMinutes() + d.getUTCSeconds() / 60;
  const g = 2 * Math.PI / days * (day - 1 + (minutes / 60 - 12) / 24);
  const eq = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g)
    - 0.014615 * Math.cos(2*g) - 0.040849 * Math.sin(2*g));
  const decl = 0.006918 - 0.399912 * Math.cos(g) + 0.070257 * Math.sin(g)
    - 0.006758 * Math.cos(2*g) + 0.000907 * Math.sin(2*g)
    - 0.002697 * Math.cos(3*g) + 0.00148 * Math.sin(3*g);
  return {minutes, eq, decl};
}
export function solarAt(terms, lon, lat, convergence = 0, aspect = 1) {
  const p = lat * radians;
  const h = ((terms.minutes + terms.eq + 4 * lon) / 4 - 180) * radians;
  const cd = Math.cos(terms.decl), sd = Math.sin(terms.decl);
  const east = -cd * Math.sin(h);
  const north = Math.cos(p) * sd - Math.sin(p) * cd * Math.cos(h);
  const up = Math.sin(p) * sd + Math.cos(p) * cd * Math.cos(h);
  const elevation = Math.asin(clamp(up, -1, 1)) / radians;
  const azimuth = (Math.atan2(east, north) / radians + 360) % 360;
  const c = Math.cos(convergence), s = Math.sin(convergence);
  const x = east*aspect*c - north*s, y = east*aspect*s + north*c;
  const n = Math.hypot(x, y) || 1;
  // Screen y points down. This rotates geographic sunlight into EPSG:3005.
  return {elevation, azimuth, dx: x/n, dy: -y/n,
    gate: smoothstep(0, 12, elevation)};
}
export function geoAt(assets, u, v) {
  const x = clamp(u) * (assets.geoWidth - 1), y = clamp(v) * (assets.geoHeight - 1);
  const ix = Math.min(Math.floor(x), assets.geoWidth-2);
  const iy = Math.min(Math.floor(y), assets.geoHeight-2);
  const a=x-ix, b=y-iy;
  const ids=[(iy*assets.geoWidth+ix)*3,(iy*assets.geoWidth+ix+1)*3,
    ((iy+1)*assets.geoWidth+ix)*3,((iy+1)*assets.geoWidth+ix+1)*3];
  return [0,1,2].map(k => (assets.geo[ids[0]+k]*(1-a)+assets.geo[ids[1]+k]*a)*(1-b)
    +(assets.geo[ids[2]+k]*(1-a)+assets.geo[ids[3]+k]*a)*b);
}
export function illuminationGrid(assets, time) {
  const terms=solarTerms(time), values=[];
  for(let i=0;i<assets.geo.length;i+=3)
    values.push(solarAt(terms, assets.geo[i], assets.geo[i+1], assets.geo[i+2], assets.geoAspect?.[i/3] ?? 1));
  return {terms, values};
}
// Constant-work box filter, edge-replicated. Radius follows source resolution.
export function boxBlur(src, w, h, radius) {
  const r=Math.max(1,Math.round(radius)), size=2*r+1;
  const tmp=new Float32Array(src.length), out=new Float32Array(src.length);
  for(let y=0;y<h;y++) {
    const row=y*w; let sum=0;
    for(let k=-r;k<=r;k++) sum+=src[row+clamp(k,0,w-1)];
    for(let x=0;x<w;x++) {
      tmp[row+x]=sum/size;
      sum+=src[row+clamp(x+r+1,0,w-1)]-src[row+clamp(x-r,0,w-1)];
    }
  }
  for(let x=0;x<w;x++) {
    let sum=0;
    for(let k=-r;k<=r;k++) sum+=tmp[clamp(k,0,h-1)*w+x];
    for(let y=0;y<h;y++) {
      out[y*w+x]=sum/size;
      sum+=tmp[clamp(y+r+1,0,h-1)*w+x]-tmp[clamp(y-r,0,h-1)*w+x];
    }
  }
  return out;
}
export function sample(src,w,h,x,y) {
  x=clamp(x,0,w-1); y=clamp(y,0,h-1);
  const x0=Math.floor(x),y0=Math.floor(y),x1=Math.min(w-1,x0+1),y1=Math.min(h-1,y0+1);
  const a=x-x0,b=y-y0;
  return (src[y0*w+x0]*(1-a)+src[y0*w+x1]*a)*(1-b)
    +(src[y1*w+x0]*(1-a)+src[y1*w+x1]*a)*b;
}
export function prepareLuminance(rgba,w,h) {
  const y=new Float32Array(w*h);
  for(let i=0;i<y.length;i++) y[i]=(rgba[i*4]*.2126+rgba[i*4+1]*.7152+rgba[i*4+2]*.0722)/255;
  const scale=w/1920;
  return {y, fine:boxBlur(y,w,h,Math.max(1,2*scale)),
    broad:boxBlur(boxBlur(y,w,h,9*scale),w,h,9*scale)};
}
// No warping, generated texture, cast shadows, height field, or surface normals.
// The optional relief term is a bounded 2D directional cue, not cloud geometry.
export function renderTreatment(rgba,w,h,assets,time,style='natural',prepared=null) {
  const {y,fine,broad}=prepared || prepareLuminance(rgba,w,h);
  const out=new Uint8ClampedArray(rgba.length), baseline=new Uint8ClampedArray(rgba.length);
  const {values}=illuminationGrid(assets,time);
  const gw=assets.geoWidth, gh=assets.geoHeight;
  let daylight=0, fullNight=0, maxShift=0;
  const distance=Math.max(1.5,5*w/1920);
  for(let row=0;row<h;row++) {
    const gy=(row+.5)/h*(gh-1), iy=Math.min(gh-2,Math.floor(gy)), by=gy-iy;
    for(let col=0;col<w;col++) {
      const i=row*w+col, k=i*4, lum=y[i];
      const gx=(col+.5)/w*(gw-1), ix=Math.min(gw-2,Math.floor(gx)), ax=gx-ix;
      const v0=values[iy*gw+ix],v1=values[iy*gw+ix+1],v2=values[(iy+1)*gw+ix],v3=values[(iy+1)*gw+ix+1];
      const w0=(1-ax)*(1-by),w1=ax*(1-by),w2=(1-ax)*by,w3=ax*by;
      // Interpolate elevation, then re-evaluate gate: zero at the interpolated horizon.
      const elevation=v0.elevation*w0+v1.elevation*w1+v2.elevation*w2+v3.elevation*w3;
      const gate=smoothstep(0,12,elevation);
      const dx=v0.dx*w0+v1.dx*w1+v2.dx*w2+v3.dx*w3;
      const dy=v0.dy*w0+v1.dy*w1+v2.dy*w2+v3.dy*w3;
      const n=Math.sqrt(dx*dx+dy*dy)||1;
      daylight+=gate; if(elevation<=0) fullNight++;
      const rr=rgba[k]/255,gg=rgba[k+1]/255,bb=rgba[k+2]/255;
      // Vivid-like fixed grade is itself gated, preserving supplied IR at night.
      // No frame-wise histogram normalization or automatic exposure pumping.
      const detail=clamp(.42*(lum-broad[i])+.38*(lum-fine[i]),-.045,.045);
      const tone=.16*(lum-.5)*(1-(2*lum-1)**2);
      const sat=.20*(4*lum*(1-lum));
      const br=clamp(rr+gate*(sat*(rr-lum)+tone+detail));
      const bg=clamp(gg+gate*(sat*(gg-lum)+tone+detail));
      const bb2=clamp(bb+gate*(sat*(bb-lum)+tone+detail));
      let shift=0;
      if(gate>0 && style!=='reference') {
        const along=sample(fine,w,h,col+dx/n*distance,row+dy/n*distance);
        const away=sample(fine,w,h,col-dx/n*distance,row-dy/n*distance);
        const chroma=Math.max(rr,gg,bb)-Math.min(rr,gg,bb);
        // Bright neutral texture receives more detail; this is not a cloud mask.
        const neutral=smoothstep(.25,.72,lum)*(1-smoothstep(.08,.30,chroma));
        const local=clamp(.58*(lum-broad[i])+.38*(fine[i]-(along+away)/2),-.055,.055);
        const cue=style==='relief' ? clamp((away-along)*.23,-.032,.032)*neutral : 0;
        shift=gate*clamp((.42+.58*neutral)*local+cue,-.060,.060);
        // Protect white crests/black water from extra clipping, without a colour tint.
        if(shift>0) shift*=1-.55*Math.max(br,bg,bb2);
        else shift*=.35+.65*lum;
      }
      maxShift=Math.max(maxShift,Math.abs(shift));
      baseline[k]=Math.round(br*255);baseline[k+1]=Math.round(bg*255);baseline[k+2]=Math.round(bb2*255);
      out[k]=Math.round(clamp(br+shift)*255);out[k+1]=Math.round(clamp(bg+shift)*255);out[k+2]=Math.round(clamp(bb2+shift)*255);
      baseline[k+3]=out[k+3]=rgba[k+3];
    }
  }
  return {out,baseline,daylightMean:daylight/(w*h),nightFraction:fullNight/(w*h),maxShift};
}
