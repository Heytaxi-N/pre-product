// 自动生成, 供应商配置来自 config.json
// === 粘贴到 szwego 页面的 devtools console 里跑 ===
(async()=>{
const SUP={"测试供货商": "_d_test_album"};
const clean=it=>({goods_id:it.goods_id,title:it.title||'',imgsSrc:it.imgsSrc||[],time_stamp:it.time_stamp,update_time:it.update_time,videoUrl:it.videoUrl||it.videoURL||''});
const filt=arr=>arr.filter(i=>!i.isTop&&!i.forwardTime&&i.parent_goods_id===i.goods_id).map(clean);
const anchorClean=arr=>arr.filter(i=>!i.isTop).map(clean);
const itemDate=it=>{const d=new Date(it.time_stamp),p=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate());};
const fetchOne=async(aid,pageTs)=>{
  const u='https://www.szwego.com/album/personal/new?&albumId='+aid+'&searchValue=&searchImg=&startDate=&endDate=&sourceId=&requestDataType='+(pageTs?'&slipType=1&timestamp='+pageTs:'');
  const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'},body:''});return await r.json();
};
const fetchDaily=async aid=>{
  const all=[],seen=new Set();let pageTs='',pages=0,latestDate='',foundLatest=false,misses=0,hasMore=false;const DAILY_MAX=50;
  while(pages<DAILY_MAX){
    const d=await fetchOne(aid,pageTs);
    const raw=d.result&&d.result.items?d.result.items:[];
    if(raw.length===0)break;
    const items=filt(raw);
    if(!latestDate&&items.length)latestDate=items.map(itemDate).sort().pop();
    const hasLatest=!!latestDate&&items.some(i=>itemDate(i)===latestDate);
    if(hasLatest){foundLatest=true;misses=0;}else if(foundLatest){misses++;}
    for(const it of items){if(!seen.has(it.goods_id)){seen.add(it.goods_id);all.push(it);}}
    pages++;
    hasMore=!!(d.result.pagination&&d.result.pagination.isLoadMore);
    if(foundLatest&&misses>=2)break;
    if(!hasMore)break;
    pageTs=d.result.pagination.pageTimestamp;
    await new Promise(r=>setTimeout(r,150));
  }
  if(pages===DAILY_MAX&&hasMore&&misses<2)throw new Error('日常抓取达到分页上限');
  return all;
};
const dl=(data,fname)=>{
  const blob=new Blob([JSON.stringify(data)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=fname;
  document.body.appendChild(a);a.click();document.body.removeChild(a);
};
const params=new URLSearchParams(location.search);
const anchorSup=params.get('anchor_supplier');
const anchorDate=params.get('anchor_date');
const anchorCode=params.get('anchor_code');
const rangeStart=params.get('range_start');
const rangeEnd=params.get('range_end');
const rangeDate=params.get('range_date');
if(anchorSup&&(anchorDate||anchorCode||(rangeStart&&rangeEnd&&rangeDate))){
  let aid=null,name=anchorSup;
  for(const [n,a] of Object.entries(SUP)){if(n===anchorSup||n.includes(anchorSup)){aid=a;name=n;break;}}
  if(!aid){console.log('未找到供货商: '+anchorSup);return;}
  const all=[];let pageTs='',pages=0,rawCount=0,hasMore=false,incomplete=false,foundCode=false,missesAfterCode=0,foundDate=false,missesAfterDate=0,foundRangeDate=false,missesAfterRangeDate=0,stopReason='end';const MAX=50;const RANGE_MAX=50;const pageLimit=rangeDate?RANGE_MAX:MAX;
  while(pages<pageLimit){
    let d;try{d=await fetchOne(aid,pageTs);}catch(e){incomplete=true;stopReason='network';break;}
    const rawItems=d.result&&d.result.items?d.result.items:[];
    rawCount+=rawItems.length;
    const items=(anchorDate||rangeStart)?anchorClean(rawItems):filt(rawItems);
    if(rawItems.length===0)break;
    const rangeItems=rangeDate?items.filter(i=>itemDate(i)===rangeDate):items;
    const pageHasCode=anchorCode&&items.some(i=>(i.title||'').includes(anchorCode));
    const pageHasDate=anchorDate&&items.some(i=>itemDate(i)===anchorDate);
    const pageHasRangeDate=rangeDate&&items.some(i=>itemDate(i)===rangeDate);
    if(pageHasCode){foundCode=true;missesAfterCode=0;}else if(anchorCode&&foundCode){missesAfterCode++;}
    if(pageHasDate){foundDate=true;missesAfterDate=0;}else if(anchorDate&&foundDate){missesAfterDate++;}
    if(pageHasRangeDate){foundRangeDate=true;missesAfterRangeDate=0;}else if(rangeDate&&foundRangeDate){missesAfterRangeDate++;}
    all.push(...rangeItems);pages++;
    hasMore=!!(d.result.pagination&&d.result.pagination.isLoadMore);
    if(anchorCode&&foundCode&&missesAfterCode>=2){stopReason='boundary';break;}
    if(anchorDate&&foundDate&&missesAfterDate>=2){stopReason='date-boundary';break;}
    if(rangeDate&&foundRangeDate&&missesAfterRangeDate>=2){stopReason='date-boundary';break;}
    if(!hasMore)break;
    pageTs=d.result.pagination.pageTimestamp;
    await new Promise(r=>setTimeout(r,150));
  }
  if(pages===pageLimit&&hasMore&&stopReason==='end'){incomplete=true;stopReason='limit';}
  dl({supplier:name,albumId:aid,items:all,anchor:{supplier:anchorSup,date:anchorDate,code:anchorCode,rangeDate,rangeStart,rangeEnd,pages,rawCount,foundDate,incomplete,stopReason,fullScan:false,dateWindow:!!anchorDate,dateScan:!!rangeDate}},'scrape_anchor.json');
  const target=anchorCode||anchorDate||(rangeDate+' '+rangeStart+' → '+rangeEnd);
  console.log((incomplete?'深挖未完成':'深挖完成')+': '+name+' '+target+' 共 '+all.length+' 条 ('+pages+' 页)\n下载 scrape_anchor.json');
  return;
}
const out={data:{}};let ok=0,err=0;
for(const [name,aid] of Object.entries(SUP)){
  try{out.data[aid]={supplier:name,items:await fetchDaily(aid)};ok++;}
  catch(e){out.data[aid]={supplier:name,items:[]};err++;}
  await new Promise(r=>setTimeout(r,200));
}
dl(out,'scrape_all.json');
console.log('抓取完成: '+ok+' 成功, '+err+' 失败\n下载 scrape_all.json');
})();