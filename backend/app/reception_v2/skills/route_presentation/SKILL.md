---
name: route-presentation
description: Present a selected route in layers, using approved itinerary and materials in an order that builds interest without flooding the visitor.
---

# 完整线路介绍

适用于客户明确想了解某条线路、索取完整介绍、行程图或继续追问住宿、车辆和亮点。

- 使用 `get_route_details` 读取客户当前关注的主题；需要资料时使用 `get_route_material_packet`，不要自己拼素材 key。
- 完整介绍先交付线路定位、天数、主要路线和行程图，再根据客户关注补充景点亮点、住宿、车辆和供氧。
- 行程图必须先于景点、住宿和车辆照片；同一素材不能重复交付。
- 分层介绍不等于省略完整线路：客户明确索取完整介绍时给出完整骨架，后续再补充细节。
- 客户插入价格、住宿、体力或证件问题时，先回答新问题，再决定是否继续未完成的介绍。
- presentation 用于呈现路线比较、行程、详情和资料范围；实际附件交付仍由服务端计划决定。
- 不把供氧、住宿或车辆配置扩展成医疗保证、实时余位或未发布承诺。

## 结束条件

客户已经拿到当前请求的线路骨架或资料，且知道可以继续比较哪一项；若已有兴趣但还需要顾问确认，则交给 `lead-handoff` 或留资判断。
