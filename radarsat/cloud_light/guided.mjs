import {boxBlur, clamp} from './solar.mjs';
import {prepareLight, applyLight, limitContrast} from './lighting.mjs';

export function prepareGuided(rgba,w,h,geo,rect,time) {
  const p=prepareLight(rgba,w,h,geo,rect,time),scale=w/(rect[2]*1920);
  const blurred=r=>boxBlur(boxBlur(p.y,w,h,Math.max(1,r*scale)),w,h,Math.max(1,r*scale));
  const b3=blurred(3),b8=blurred(8),b24=blurred(24);
  const small=new Float32Array(w*h),broad=new Float32Array(w*h);
  for(let i=0;i<small.length;i++){small[i]=b3[i]-b8[i];broad[i]=b8[i]-b24[i];}
  return {...p,small,broad};
}

function channelTables(gains) {
  const maps=[],lower=[],upper=[];
  for(let c=0;c<3;c++) {
    const map=new Int16Array(129),lo=new Int16Array(256),hi=new Int16Array(256);
    for(let d=-64;d<=64;d++)map[d+64]=Math.round(d*(d<0?gains.shadow[c]:gains.highlight[c]));
    for(let v=0;v<256;v++) {
      let a=0,b=0;
      for(let d=-64;d<=64;d++){
        const out=v+map[d+64];
        if(out>=(v===0?0:1)&&out<=(v===255?255:254)){a=Math.min(a,d);b=Math.max(b,d);}
      }
      lo[v]=a;hi[v]=b;
    }
    maps.push(map);lower.push(lo);upper.push(hi);
  }
  return {maps,lower,upper};
}

export function renderGuided(p,recipe,mode='guided',strength=.85,colour=true) {
  if(mode==='previous')return applyLight(p,strength);
  const gains=colour?recipe.channelGains:{shadow:[1,1,1],highlight:[1,1,1]};
  const {maps,lower,upper}=channelTables(gains),shifts=new Int16Array(p.w*p.h);
  const coefs=recipe.luminanceCoefficients;
  let requestedAbsolute=0;
  for(let i=0;i<shifts.length;i++) {
    const k=i*4,weight=p.eligibility[i];if(!weight)continue;
    const shape=mode==='guided'?
      1.7*(coefs[0]*p.small[i]+coefs[1]*p.broad[i])+1.9*p.sunCue[i]:
      1.35*p.small[i]+2.8*p.broad[i]+2.1*p.sunCue[i];
    const lo=Math.max(lower[0][p.rgba[k]],lower[1][p.rgba[k+1]],lower[2][p.rgba[k+2]]);
    const hi=Math.min(upper[0][p.rgba[k]],upper[1][p.rgba[k+1]],upper[2][p.rgba[k+2]]);
    const requested=clamp(shape,-52,52)*weight*clamp(strength);
    shifts[i]=Math.round(clamp(requested,lo,hi));requestedAbsolute+=Math.abs(shifts[i]);
  }
  const convergence=limitContrast(shifts,p.y,p.w,p.h),out=new Uint8ClampedArray(p.rgba);
  let changed=0,protectedCount=0,maxShift=0,retainedAbsolute=0;
  for(let i=0;i<shifts.length;i++) {
    const k=i*4,d=shifts[i];if(d)changed++;if(p.protectedPixels[i])protectedCount++;
    retainedAbsolute+=Math.abs(d);
    for(let c=0;c<3;c++){const delta=maps[c][d+64];out[k+c]+=delta;maxShift=Math.max(maxShift,Math.abs(delta));}
  }
  return {out,shifts,stats:{changed,protectedCount,total:shifts.length,maxShift,requestedAbsolute,retainedAbsolute,...convergence}};
}
