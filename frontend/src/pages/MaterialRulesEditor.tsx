import {useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {api, API_BASE} from '../api';
import MaterialPicker from './MaterialPicker';
import {MaterialRule} from './reception-config';

export default function MaterialRulesEditor({rules, routes, routeNames = {}, inboxes, onChange}: {
  rules: MaterialRule[]; routes: string[]; inboxes: {id: number; name: string}[];
  routeNames: Record<string,string>;
  onChange: (rules: MaterialRule[]) => void;
}) {
  const [inbox, setInbox] = useState<number>();
  const selectedInbox=inbox ?? inboxes[0]?.id;
  const catalog=useQuery({queryKey:['rule-materials',selectedInbox],queryFn:()=>api<{items:{key:string;name:string;media_id:number|null}[]}>(`/materials?inbox_binding_id=${selectedInbox}`),enabled:!!selectedInbox});
  const update = (index: number, changes: Partial<MaterialRule>) => onChange(rules.map((r,i) => i === index ? {...r,...changes} : r));
  return <section className="strategy-section-stack">
    <h3>场景图库与跟进素材</h3>
    <p>先限定线路和用途，再描述适用问题。标签留空时由 AI 根据对话判断；填写标签时，还需命中其中一个标签。每次从该规则的未发图片中选一张，没有可用图片则只发文字。</p>
    <label className="block-field">素材所属收件夹<select value={selectedInbox ?? ''} onChange={e=>setInbox(Number(e.target.value))}><option value="">请选择</option>{inboxes.map(i=><option key={i.id} value={i.id}>{i.name}</option>)}</select></label>
    {rules.map((rule,index)=><fieldset key={rule.id} className="reply-expression-editor">
      <label><input type="checkbox" checked={rule.enabled} onChange={e=>update(index,{enabled:e.target.checked})}/>启用</label>
      <label className="block-field">名称<input value={rule.name} onChange={e=>update(index,{name:e.target.value})}/></label>
      <label className="block-field">用途<select value={rule.purpose} onChange={e=>update(index,{purpose:e.target.value as MaterialRule['purpose']})}><option value="reply">客户提问时</option><option value="followup">沉默跟进时</option><option value="both">两者都可</option></select></label>
      <div>适用线路{routes.map(route=><label key={route}><input type="checkbox" checked={rule.route_variants.includes(route)} onChange={e=>update(index,{route_variants:e.target.checked ? [...rule.route_variants,route] : rule.route_variants.filter(r=>r!==route)})}/>{routeNames[route] ?? route}</label>)}</div>
      <label className="block-field">客户标签（每行一个，可留空）<textarea value={rule.any_labels.join('\n')} onChange={e=>update(index,{any_labels:e.target.value.split('\n')})}/></label>
      <label className="block-field">适用场景与不适用情况<textarea value={rule.scenario} onChange={e=>update(index,{scenario:e.target.value})}/></label>
      <label className="block-field">业务确认的说明文字<textarea rows={4} value={rule.approved_text} onChange={e=>update(index,{approved_text:e.target.value})}/></label>
      <div>{rule.asset_keys.map((key,i)=>{const item=catalog.data?.items.find(m=>m.key===key);return <button type="button" key={key} title="移除图片" onClick={()=>update(index,{asset_keys:rule.asset_keys.filter(k=>k!==key)})}>{item?.media_id && <img width={80} src={`${API_BASE}/media/${item.media_id}/preview`} alt={item.name}/>} {item?.name ?? `已选图片 ${i+1}`} ×</button>;})}</div>
      <MaterialPicker inboxId={selectedInbox} route={rule.route_variants[0] ?? ''} onSelect={item=>{if(!rule.asset_keys.includes(item.key)) update(index,{asset_keys:[...rule.asset_keys,item.key]});}}/>
      <details><summary>预览配置</summary><p>{rule.approved_text}</p><p>{rule.asset_keys.length ? `候选图片 ${rule.asset_keys.length} 张，每次最多一张，实际使用前仍检查线路、标签和去重。` : '仅文字'}</p></details>
      <button type="button" className="secondary-button" onClick={()=>onChange(rules.filter((_,i)=>i!==index))}>删除规则</button>
    </fieldset>)}
    <button type="button" className="secondary-button" disabled={rules.length>=100} onClick={()=>onChange([...rules,{id:crypto.randomUUID(),name:'新素材规则',enabled:false,purpose:'reply',route_variants:routes.slice(0,1),any_labels:[],scenario:'',approved_text:'',asset_keys:[]}])}>新增素材规则</button>
  </section>;
}
