---
name: route-presentation
description: Present a selected route in layers, using approved itinerary and materials in an order that builds interest without flooding the visitor.
---

# 完整线路介绍

适用于客户明确想了解某条线路、索取完整介绍、行程图或继续追问住宿、车辆和亮点。

客户只选定线路时，先执行线路 Skill 的接待顺序：人数未知且未问过就先问人数并等待，不直接发送行程图或住宿照片；人数已知不重复问。客户明确要求直接看资料时按其请求交付。

- 使用 `get_route_details` 读取客户当前关注的主题；需要资料时使用 `get_route_material_packet`，不要自己拼素材 key。
- 完整介绍按对应线路 Skill 和配置顺序发送整套图文；不要只发骨架后省略剩余介绍。
- 行程图必须先于景点、住宿和车辆照片；同一素材不能重复交付。
- 普通插问在整套介绍完成后集中回答；停止、改线和转人工立即处理。
- 查看已交付进度。完整介绍已经结束后，“继续看行程，另外问住宿”是继续答疑，不是要求重发；不要再次输出full_introduction。只有明确“重发整套”才设置allow_material_resend=true。
- presentation 用于呈现路线比较、行程、详情和资料范围；实际附件交付仍由服务端计划决定。
- 不把供氧、住宿或车辆配置扩展成医疗保证、实时余位或未发布承诺。

## 结束条件

客户已经拿到当前请求的线路骨架或资料，且知道可以继续比较哪一项；若已有兴趣但还需要顾问确认，则交给 `lead-handoff` 或留资判断。
