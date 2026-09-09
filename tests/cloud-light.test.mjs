import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {prepareGuided,renderGuided} from '../radarsat/cloud_light/guided.mjs';
import {auditLight,limitContrast} from '../radarsat/cloud_light/lighting.mjs';
const recipe=JSON.parse(fs.readFileSync(new URL('../radarsat/cloud_light/recipe.json',import.meta.url)));
const geo=JSON.parse(fs.readFileSync(new URL('../radarsat/cloud_light/geo.json',import.meta.url)));
test('quantized soft50 preserves protected pixels, alpha, contrast and headroom at day/dusk/night',()=>{
 const w=128,h=96,rgba=new Uint8ClampedArray(w*h*4);
 let seed=19;
 for(let row=0;row<h;row++)for(let col=0;col<w;col++){
  seed=(Math.imul(seed,1664525)+1013904223)>>>0;
  const i=(row*w+col)*4,v=Math.round(150+50*Math.sin(col/10)*Math.cos(row/17)+(seed%12));
  rgba[i]=v;rgba[i+1]=v+3;rgba[i+2]=v+7;rgba[i+3]=seed%256;
  if(col%29===0){rgba[i]=20;rgba[i+1]=70;rgba[i+2]=120;}
 }
 for(const time of ['2026-09-09T22:40:21Z','2026-09-09T02:40:21Z','2026-09-09T09:40:21Z']){
  const p=prepareGuided(rgba,w,h,geo,[0,0,1,1],time);
  const r=renderGuided(p,recipe,'layered',.5,true),a=auditLight(p,r);
  for(const key of ['protectedChanges','newClipping','alphaChanges','contrastViolations'])assert.equal(a[key],0,key);
  assert.equal(r.stats.fallback,false);
  if(time.includes('T09:'))assert.deepEqual(r.out,rgba);
 }
});
test('compiled constraint traversal equals original relaxation, including diagonals',()=>{
 function reference(s,y,w,h){
  function pair(a,b){const d=y[a]-y[b];if(Math.abs(d)<4)return false;
   const bright=d>0?a:b,dark=d>0?b:a;let excess=s[dark]-s[bright];if(excess<=0)return false;
   const up=Math.max(0,s[dark]),down=Math.max(0,-s[bright]);
   const take=Math.min(up,Math.ceil(excess*up/(up+down)));s[dark]-=take;excess-=take;s[bright]+=Math.min(down,excess);return true;}
  for(let pass=0;pass<80;pass++){let changed=false;
   for(let row=0;row<h;row++)for(let col=0;col<w;col++){const i=row*w+col;
    if(col+1<w)changed=pair(i,i+1)||changed;
    if(row+1<h){changed=pair(i,i+w)||changed;if(col)changed=pair(i,i+w-1)||changed;if(col+1<w)changed=pair(i,i+w+1)||changed;}}
   if(!changed)return;
  }s.fill(0);
 }
 let seed=7;
 for(let trial=0;trial<12;trial++){
  const w=43,h=31,y=new Float64Array(w*h),s=new Int16Array(w*h);
  for(let i=0;i<s.length;i++){seed=(Math.imul(seed,1664525)+1013904223)>>>0;y[i]=seed%256;s[i]=seed%3===0?0:(seed%53)-26;}
  const expected=s.slice();reference(expected,y,w,h);limitContrast(s,y,w,h);assert.deepEqual(s,expected);
 }
});
