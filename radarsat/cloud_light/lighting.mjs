import {boxBlur, sample, clamp, smoothstep, illuminationGrid} from './solar.mjs';

// All colour values remain at their original pixel indices. The blurred fields
// drive an additive lighting cue; they are never used as replacement texture.
export function prepareLight(rgba,w,h,assets,rect,time) {
  const n=w*h, y=new Float64Array(n), mask=new Uint8Array(n);
  const distance=new Uint16Array(n), lo=new Int16Array(n),hi=new Int16Array(n);
  for(let i=0;i<n;i++) {
    const k=4*i,r=rgba[k],g=rgba[k+1],b=rgba[k+2];
    y[i]=.2126*r+.7152*g+.0722*b;
    const mn=Math.min(r,g,b),mx=Math.max(r,g,b);
    mask[i]=y[i]>92 && mx-mn<48 ? 1:0;
    distance[i]=mask[i]?Math.min(1000,i%w+1,w-i%w,Math.floor(i/w)+1,h-Math.floor(i/w)):0;
    lo[i]=mn===0?0:1-mn;hi[i]=mx===255?0:254-mx;
  }
  // Chebyshev distance to the appearance-mask boundary, including image edges.
  for(let row=0;row<h;row++)for(let col=0;col<w;col++) {
    const i=row*w+col;if(!distance[i])continue;
    if(col)distance[i]=Math.min(distance[i],distance[i-1]+1);
    if(row){distance[i]=Math.min(distance[i],distance[i-w]+1);
      if(col)distance[i]=Math.min(distance[i],distance[i-w-1]+1);
      if(col+1<w)distance[i]=Math.min(distance[i],distance[i-w+1]+1);}
  }
  for(let row=h-1;row>=0;row--)for(let col=w-1;col>=0;col--) {
    const i=row*w+col;if(!distance[i])continue;
    if(col+1<w)distance[i]=Math.min(distance[i],distance[i+1]+1);
    if(row+1<h){distance[i]=Math.min(distance[i],distance[i+w]+1);
      if(col)distance[i]=Math.min(distance[i],distance[i+w-1]+1);
      if(col+1<w)distance[i]=Math.min(distance[i],distance[i+w+1]+1);}
  }
  const scale=w/(rect[2]*1920),r=Math.max(1,Math.round(6*scale));
  const broad=boxBlur(boxBlur(y,w,h,r),w,h,r);

  const large=boxBlur(boxBlur(y,w,h,Math.max(2,18*scale)),w,h,Math.max(2,18*scale));
  const grid=illuminationGrid(assets,time), gw=assets.geoWidth,gh=assets.geoHeight;
  const desired=new Float32Array(n),protectedPixels=new Uint8Array(n);
  const eligibility=new Float32Array(n),sunCue=new Float32Array(n);
  for(let row=0;row<h;row++)for(let col=0;col<w;col++) {
    const i=row*w+col;
    const u=rect[0]+(col+.5)/w*rect[2],v=rect[1]+(row+.5)/h*rect[3];
    const gx=clamp(u)*(gw-1),gy=clamp(v)*(gh-1);
    const ix=Math.min(gw-2,Math.floor(gx)),iy=Math.min(gh-2,Math.floor(gy));
    const a=gx-ix,b=gy-iy,ids=[iy*gw+ix,iy*gw+ix+1,(iy+1)*gw+ix,(iy+1)*gw+ix+1];
    const weights=[(1-a)*(1-b),a*(1-b),(1-a)*b,a*b];
    let dx=0,dy=0,el=0;
    for(let j=0;j<4;j++){const s=grid.values[ids[j]];dx+=s.dx*weights[j];dy+=s.dy*weights[j];el+=s.elevation*weights[j];}
    const gate=smoothstep(0,12,el),edge=smoothstep(3*scale,11*scale,distance[i]);
    protectedPixels[i]=edge===0||gate===0?1:0;
    const norm=Math.hypot(dx,dy)||1,d=6*scale;
    const toward=sample(broad,w,h,col+dx/norm*d,row+dy/norm*d);
    const away=sample(broad,w,h,col-dx/norm*d,row-dy/norm*d);
    eligibility[i]=edge*gate;sunCue[i]=away-toward;
    const cue=.9*(broad[i]-large[i])+.8*(away-toward);
    desired[i]=clamp(cue,-36,36)*edge*gate;

  }
  return {y,desired,protectedPixels,lo,hi,w,h,rgba,eligibility,sunCue};
}

// A limiter only moves proposed shifts toward zero. For adjacent source
// differences >= 4 luminance levels, retain at least 100% of the original
// signed contrast (including diagonals). Zero is always a feasible solution.
export function limitContrast(shifts,y,w,h) {
  // A zero shift can never become nonzero. Compile only potentially active
  // constraints once, preserving the reference's exact traversal order.
  const brightEdges=[],darkEdges=[];
  const add=(a,b)=>{
    if(!shifts[a]&&!shifts[b])return;
    const d=y[a]-y[b];if(Math.abs(d)<4)return;
    brightEdges.push(d>0?a:b);darkEdges.push(d>0?b:a);
  };
  for(let row=0;row<h;row++)for(let col=0;col<w;col++){
    const i=row*w+col;
    if(col+1<w)add(i,i+1);
    if(row+1<h){add(i,i+w);if(col)add(i,i+w-1);if(col+1<w)add(i,i+w+1);}
  }
  const bright=Int32Array.from(brightEdges),dark=Int32Array.from(darkEdges);
  let passes=0,changes=0;
  for(;passes<80;passes++) {
    changes=0;
    for(let e=0;e<bright.length;e++){
      const b=bright[e],d=dark[e];let excess=shifts[d]-shifts[b];
      if(excess<=0)continue;
      const up=Math.max(0,shifts[d]),down=Math.max(0,-shifts[b]);
      const takeUp=Math.min(up,Math.ceil(excess*up/(up+down)));
      shifts[d]-=takeUp;excess-=takeUp;
      shifts[b]+=Math.min(down,excess);changes++;
    }
    if(!changes)break;
  }
  // Fail closed if the bounded relaxation has not converged.
  const fallback=changes>0;if(fallback)shifts.fill(0);
  return {passes:passes+1,fallback};
}

export function applyLight(prepared,strength=1) {
  const {rgba,w,h,y,desired,protectedPixels,lo,hi}=prepared;
  const shifts=new Int16Array(w*h),out=new Uint8ClampedArray(rgba);
  for(let i=0;i<shifts.length;i++)shifts[i]=protectedPixels[i]?0:Math.round(clamp(desired[i]*clamp(strength),lo[i],hi[i]));
  const convergence=limitContrast(shifts,y,w,h);
  let changed=0,protectedCount=0,maxShift=0;
  for(let i=0;i<shifts.length;i++){
    const d=shifts[i],k=4*i;if(d)changed++;if(protectedPixels[i])protectedCount++;
    maxShift=Math.max(maxShift,Math.abs(d));
    out[k]+=d;out[k+1]+=d;out[k+2]+=d;
  }
  return {out,shifts,stats:{changed,protectedCount,total:w*h,maxShift,...convergence}};
}

export function auditLight(prepared,result) {
  const {rgba,w,h,y,protectedPixels}=prepared,{out}=result;
  let protectedChanges=0,newClipping=0,alphaChanges=0,contrastViolations=0,minRetention=1,checkedPairs=0;
  const outputY=i=>.2126*out[i*4]+.7152*out[i*4+1]+.0722*out[i*4+2];
  for(let i=0;i<w*h;i++){
    const k=i*4;
    if(protectedPixels[i] && [0,1,2].some(c=>rgba[k+c]!==out[k+c]))protectedChanges++;
    for(let c=0;c<3;c++)if((out[k+c]===0&&rgba[k+c]!==0)||(out[k+c]===255&&rgba[k+c]!==255))newClipping++;
    if(out[k+3]!==rgba[k+3])alphaChanges++;
    const col=i%w,row=Math.floor(i/w),pairs=[];
    if(col+1<w)pairs.push(i+1);
    if(row+1<h){pairs.push(i+w);if(col)pairs.push(i+w-1);if(col+1<w)pairs.push(i+w+1);}
    for(const j of pairs){const d=y[i]-y[j];if(Math.abs(d)<4)continue;checkedPairs++;
      const ratio=(outputY(i)-outputY(j))/d;minRetention=Math.min(minRetention,ratio);if(ratio<1-1e-8)contrastViolations++;}
  }
  return {...result.stats,protectedChanges,newClipping,alphaChanges,contrastViolations,minRetention,checkedPairs};
}
