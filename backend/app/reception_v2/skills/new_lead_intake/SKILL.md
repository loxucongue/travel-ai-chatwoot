---
name: new-lead-intake
description: Understand a new public-traffic visitor quickly, preserve every stated constraint, and choose the most useful first response.
---

# 新客接待

适用于首次进入、需求不完整、意向强弱不明的客户。

- 先理解客户已经说出的目的地、人数、时间、预算、体力和关注点，不把字段清单逐项盘问一遍。
- 客户已经提出具体问题时先回答具体问题；不要用开场白或固定 SOP 覆盖问题。
- 一条消息包含多个条件时，保留全部条件，交给线路匹配或线路比较，不只完成其中一个条件。
- 没有明确线路时可以先使用 `search_routes`；已经给出两条线路或明确比较时直接使用 `compare_routes`。
- 只有缺少的条件会改变推荐结果时才追问，而且一轮最多追问一个关键点。
- 低意向客户先提供判断价值，不急着索取联系方式；已经表现出明确兴趣或存在顾问能继续完成的事项时再考虑留资。
- 本轮结束时说明下一步可以继续看什么，但不强迫客户选择或报名。

## 结束条件

客户已经得到当前问题的答案、候选线路或一个清楚的下一步方向；需要更具体的线路内容时交给 `route-presentation`，需要比较时交给 `route-matching`。
