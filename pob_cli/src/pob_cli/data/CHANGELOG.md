# Changelog

## v0.11.0

### 中文

0.11 新增功能
1.大幅优化了购物体验，现在生成全身装备的购买链接的速度大幅提升了！
2.现在自动脱去bd水分的功能更准确了
3.引入了造价估算功能，现在不用问主播主播这bd到底多少造价了
4.添加找星团功能，现在会直接找到你要的两个点都在外头的完美星团了！
5.大幅优化了装备查询体验，更精准了
6.pob的红字（不生效）词条现在也会被agent考虑进去了
7.现在更会考虑装备天赋之间的联动了
8.现在默认会按照dps排序来选取装备
9.国服账号导入之后会考虑星团和各种跳点珠宝的配置了（不是100%准确，请记得导出pob链接来确认）
10.纹身识别功能更好了，现在能精准的告诉你还有哪些小天赋点可以换成纹身
11.改进了召唤bd的计算支持
12.找到永恒珠宝的同时还能帮你立刻查价格了
修了啥
说实话，太多我看不过来，密密麻麻几十条
最重要的：
修好了快速模型会在原地兜圈子不调用pob，反而是自己重新实现了一遍本工具功能（而且写的烂完了）来应付用户需要，导致浪费天量token的问题
（我想破头都想不出来还能有这种bug）
以上就是本次更新的具体内容，本插件永久免费开源，由b站@流放降临-某雷 维护，喜欢的话来点个star吧！
项目地址：https://github.com/exilemoulei/pob4agents

### English

0.11 What's New
1. Massively improved the shopping experience — generating trade links for a full set of gear is way faster now!
2. Auto-dewatering (stripping inflated PoB configs) is now noticeably more accurate
3. Added build cost appraisal — no more asking streamers how much a build costs
4. Added Cluster Jewel finder — searches directly for perfect clusters with both target notables in the front!
5. Massively improved gear search — much sharper and more accurate
6. Red (unparsed/inactive) PoB mod lines are now considered by the agent too
7. Much better awareness of synergies between gear and the passive tree
8. Gear recommendations now default to sorting by DPS
9. CN profile imports now take Cluster Jewels and radius/jump jewels into account (not 100% exact — export a PoB link to double-check)
10. Better Tattoo recognition — accurately tells you which small passive nodes can still be swapped to Tattoos
11. Improved calculation support for Minion builds
12. Timeless Jewel search can now look up live prices right away
What Was Fixed
Honestly, way too many to list — dozens of fixes.
The biggest one:
Fixed a bug where fast models would loop in place instead of calling pob, instead trying to re-implement our tool's features themselves (and doing a terrible job of it) to answer the user, burning an astronomical number of tokens.
(I still can't wrap my head around how a bug like that could even exist.)
That's everything in this update! Permanently free and open-source, maintained by @moulei (https://www.youtube.com/@moulei) — if you like it, drop a Star on GitHub!
https://github.com/exilemoulei/pob4agents
