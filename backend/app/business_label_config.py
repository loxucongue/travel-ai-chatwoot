from typing import Literal
import re
from pydantic import BaseModel, Field, ConfigDict, model_validator
from app.operations import setting_value

KEY='business_labels'
ENGAGED='已開口'
LEGACY_ENGAGED='以開口'
ROWS=[
 ('beijing','北京',['北京']),('tibet','西藏',['西藏','拉薩','拉萨','林芝','珠峰','阿里']),
 ('seda','色達',['色達','色达']),('yunnan','雲南',['雲南','云南','昆明','麗江','丽江','大理','香格里拉']),
 ('xinjiang','新疆',['新疆','烏魯木齊','乌鲁木齐','喀什','伊犁']),
 ('shang_su_hang','上蘇杭',['上海','蘇州','苏州','杭州','上蘇杭','上苏杭']),
 ('guizhou','貴州',['貴州','贵州','貴陽','贵阳']),('zhangjiajie','張家界',['張家界','张家界']),
 ('chongqing','重慶',['重慶','重庆']),('west_sichuan','川西',['川西','稻城亞丁','稻城亚丁','稻程亞丁','稻程亚丁','色達','色达']),
 ('sichuan','四川',['四川','成都','九寨溝','九寨沟','峨眉山','峨嵋山']),
 ('fujian','福建',['福建','廈門','厦门','福州','泉州']),('guangdong','廣東',['廣東','广东','廣州','广州','深圳']),
 ('guangxi','廣西',['廣西','广西','桂林','陽朔','阳朔','南寧','南宁']),('cruise','郵輪',['郵輪','邮轮','遊輪','游轮']),
 ('qinggan','青甘',['青甘','青海','甘肅','甘肃','敦煌','西寧','西宁','青甘大環線','青甘大环线']),
 ('inner_mongolia','內蒙',['內蒙','内蒙','內蒙古','内蒙古','呼倫貝爾','呼伦贝尔']),
 ('northeast','東北',['東北','东北','哈爾濱','哈尔滨','吉林','遼寧','辽宁','黑龍江','黑龙江','長白山','长白山']),
 ('shandong','山東',['山東','山东','青島','青岛','濟南','济南']),('other','其他',[])]


class Category(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')
    label: str = Field(min_length=1,max_length=60)
    dimension: Literal['destination','travel_type']='destination'
    aliases: list[str] = Field(default_factory=list,max_length=100)
    includes: str = Field(default='',max_length=1500)
    excludes: str = Field(default='',max_length=1500)
    examples: list[str] = Field(default_factory=list,max_length=30)
    counterexamples: list[str] = Field(default_factory=list,max_length=30)
    linked: list[str] = Field(default_factory=list,max_length=10)
    enabled: bool=True


class LabelConfig(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version: int=Field(default=1,ge=1)
    enabled: bool=False
    categories: list[Category]=Field(min_length=1,max_length=100)

    @model_validator(mode='after')
    def valid_catalog(self):
        ids=[c.id for c in self.categories]; names=[c.label.strip().casefold() for c in self.categories]
        if len(ids)!=len(set(ids)) or len(names)!=len(set(names)):raise ValueError('分类编号和标签名称不能重复')
        from app.runtime_settings import BLOCK_LABELS
        reserved={'ai','ai跟进','ai跟進','人工接管','客诉','客訴','停止联系','已開口','以開口','洗單回'} | {x.casefold() for x in BLOCK_LABELS}
        if any(n in reserved for n in names):raise ValueError('不能使用接待控制或开口标签名称')
        for c in self.categories:
            if c.label!=c.label.strip() or any(not a.strip() or len(a)>120 for a in c.aliases):raise ValueError('名称或别名格式错误')
            if any(i not in ids or i==c.id for i in c.linked):raise ValueError('联动分类无效')
        def visit(cid,path):
            if cid in path:raise ValueError('联动分类不能循环')
            for child in next(c for c in self.categories if c.id==cid).linked:visit(child,path|{cid})
        for cid in ids:visit(cid,set())
        return self


def defaults():
    categories=[Category(id=i,label=n,aliases=a,dimension='travel_type' if i=='cruise' else 'destination').model_dump() for i,n,a in ROWS]
    by_id={c['id']:c for c in categories}
    by_id['seda']['linked']=['west_sichuan']
    by_id['sichuan']['excludes']='只咨询色达、稻城亚丁或川西时，不自动追加四川；明确咨询四川或成都、九寨沟、峨眉山等才选。'
    by_id['west_sichuan']['excludes']='仅咨询成都、九寨沟或峨眉山不归川西。'
    by_id['other']['includes']='明确咨询未列出的目的地；必须保留真实地名，未知或没想好不选其他。'
    return LabelConfig(categories=categories).model_dump()


def configuration(db):
    return LabelConfig.model_validate(setting_value(db,KEY,defaults())).model_dump()


def expand(ids,config):
    catalog={c['id']:c for c in config['categories']}
    result=set()
    def add(cid):
        if cid not in catalog or cid in result or not catalog[cid]['enabled']:return
        result.add(cid)
        for linked in catalog[cid]['linked']:add(linked)
    for cid in ids:add(cid)
    return sorted(result)
