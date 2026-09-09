// One source image per invocation; stdout is binary RGBA, stderr is metrics.
import fs from 'node:fs';
import {prepareGuided,renderGuided} from './guided.mjs';
import {illuminationGrid,clamp,smoothstep} from './solar.mjs';
const [width,height,time]=process.argv.slice(2), w=Number(width),h=Number(height);
if(!Number.isInteger(w)||!Number.isInteger(h)||w<2||h<2)throw Error('Invalid dimensions');
const geo=JSON.parse(fs.readFileSync(new URL('./geo.json',import.meta.url)));
const recipe=JSON.parse(fs.readFileSync(new URL('./recipe.json',import.meta.url)));
const raw=fs.readFileSync(0),n=w*h*4;
if(raw.length!==2*n)throw Error('Incomplete source/baseline payload');
const rgba=new Uint8ClampedArray(raw.subarray(n));
// Fixed daylight grade fades smoothly to supplied GeoColor/IR at night.
const grid=illuminationGrid(geo,time),gw=geo.geoWidth,gh=geo.geoHeight;
for(let row=0;row<h;row++)for(let col=0;col<w;col++){
 const gx=(col+.5)/w*(gw-1),gy=(row+.5)/h*(gh-1);
 const ix=Math.min(gw-2,Math.floor(gx)),iy=Math.min(gh-2,Math.floor(gy));
 const a=gx-ix,b=gy-iy,vs=grid.values;
 const el=(vs[iy*gw+ix].elevation*(1-a)+vs[iy*gw+ix+1].elevation*a)*(1-b)
 +(vs[(iy+1)*gw+ix].elevation*(1-a)+vs[(iy+1)*gw+ix+1].elevation*a)*b;
 const gate=smoothstep(0,12,el),k=(row*w+col)*4;
 for(let c=0;c<3;c++)rgba[k+c]=Math.round(raw[k+c]+gate*(rgba[k+c]-raw[k+c]));
}
const start=performance.now();
const p=prepareGuided(rgba,w,h,geo,[0,0,1,1],time);
const result=renderGuided(p,recipe,'layered',.5,true);
if(result.stats.fallback)throw Error('Cloud contrast safeguard did not converge');
fs.writeFileSync(1,result.out);
console.error(JSON.stringify({filterMs:performance.now()-start,...result.stats}));
