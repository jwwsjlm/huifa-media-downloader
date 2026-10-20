# 4K 压缩步骤与 GitHub 方案调研

调研日期：2026-10-20。本文是实施前的调研快照，源码行号对应 `d8625dd` 的严格目标大小策略。后续采用近似体积、单次编码和并行源检查，实施与实测结果见 [近似体积流程报告](PERFORMANCE_APPROXIMATE_SIZE_2026-10-20.md)；下文其他候选参数尚未在本项目实测。

## 本项目实测根因

这条 39分44秒的 4K/50fps 视频：源分析 8分04秒，首轮编码 17分43秒，体积超限后再次编码 17分42秒，输出验证 5分19秒，总计 48分59秒。首轮输出 4,272,312,113 字节，超过 4,115,327,875 字节目标 3.81%；码率由 13,402,129 降至 12,265,247 bps 后重做，最终为 3,928,400,598 字节。两轮都从原视频读取。

当前预算已扣除音轨等固定流，并预留至少 2% 封装余量（`compression.rs:983,1027,1851`），不能把改进描述成补一个原本缺失的公式。此次仍超限，说明需要针对编码器实际码率偏差校准；FFmpeg 默认还有 VBV 配置，未显式传参数不等于完全没有码率控制。超标重试在 `compression.rs:1997`，原始首轮大小在 `output/performance-20261020/after-170701-b8d876be-adaa-4a92-9876-83abb035fb5c/data/task-logs/80e3ff1f-7117-4c2d-a9e3-4309d015bf5a.log:49802`。

源分析约 479 秒用于完整帧解码，约 4 秒用于包统计。检查后段 HDR10+/Dolby Vision、隔行、尺寸/像素/色彩变化、坏帧和时间轴，不是为了算出最佳压缩画质。完整分析缓存已经存在，但仅同进程最多 16 项，不跨启动保存（`compression.rs:797,966`）。

## 能省什么

1. **优先减少超标后整片重编码。** FFmpeg 将 `-b:v` 传给 NVENC 的平均码率；未指定 `-rc` 时默认走 VBR，所以加一项 `-rc vbr` 本身不会解决超标。`-maxrate` 和 `-bufsize` 控制峰值与缓冲，也不是成品文件字节数上限。[FFmpeg 源码](https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/nvenc.c)
2. **去掉不必要的 GPU→CPU→GPU 回传。** NVIDIA 官方路径让 CUDA 解码帧留在显存，直接送 NVENC；需要缩放时用 `scale_cuda`。本项目当前 `hwdownload,format=nv12,scale=iw:ih,...` 值得做同素材对比，但必须验证旋转、色彩、尺寸和帧率语义。[NVIDIA FFmpeg 指南](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/ffmpeg-with-nvidia-gpu/index.html)
3. **考虑缩短预分析，而非删除检查。** HandBrake 的 `DecodePreviews` 在分散位置抽样，至少取 3 个预览，证明全片预解码不是通用硬性要求。抽样可能漏掉后段 HDR、帧率或色彩变化，不能声称等价于全片检查。[HandBrake scan.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/scan.c)

## 为什么会编码两次

此次记录是“首个成品体积超限 → 降低平均码率 → 从头重做”，属于应用的体积重试。它和 NVENC 的 `-multipass qres/fullres` 不同：后者在一次文件编码中逐帧分析与分配码率。HandBrake 维护者也明确说明它仍显示为一次编码。[HandBrake 讨论 #5594](https://github.com/HandBrake/HandBrake/discussions/5594)

NVENC VBR 尝试贴近长期平均码率；CQ 则优先目标画质，体积随内容变化。`qres` 的首阶段使用四分之一分辨率，可能改善帧内码率分配和目标码率贴合度，但会降低吞吐，且无法保证最后容器文件字节数刚好达标。[NVIDIA 码率控制与逐帧多遍说明](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/nvenc-video-encoder-api-prog-guide/index.html#rate-control)

## 最小可行改法

- **校准现有体积预算**：现有公式为 `视频码率 = 8 × (目标字节数 − 固定流预算 − 封装余量) / 时长`；在此基础上按真实素材的 NVENC 偏差试验安全系数，保留成品大小检查和失败兜底。余量越大，一次成功机会越高，但画质或目标体积利用率会下降。这是本项目可验证的工程方案，并非外部项目的保证。
- **参数实验只改一组变量**：在现有编码、音频与色彩参数上比较 `-rc vbr -b:v N -maxrate M -bufsize B`；另测 `-multipass qres`。其中 `M/B` 需要实测选定，不能用任意倍数保证文件大小。FFmpeg 确实公开了 `disabled/qres/fullres`，默认 `disabled`。[FFmpeg HEVC 选项](https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/nvenc_hevc.c)
- **想保画质再做短片试编码**：ab-av1 用多个短样本搜索 CRF，输出预计成品大小和耗时，最后才全片编码；可借鉴“先用短样本排除明显超标参数”。它得到的是预测，不能保证严格大小，且会增加样本编码与质量评估成本。[ab-av1](https://github.com/alexheretic/ab-av1)
- **保留源文件与最终校验**：ab-av1 也提供整片解码校验和时长校验，因为编码器成功退出仍可能得到损坏或截短文件。可研究在正常编码中收集源检查信息，减少重复读取；把完整输出校验改成抽样，会降低发现局部损坏的能力。[ab-av1 的 verify](https://github.com/alexheretic/ab-av1#command-encode)

优先级：预算校准与减少重试 → 显存直通实测 → 普通素材快速分析路径。当前没有新的性能数字；不可据此承诺总耗时。

只按现有记录减掉一次重试，48分59秒约变成 31分18秒；这只是算术估计，不是新方案实测。若再更改全片预分析，必须说明抽样会缩小检查范围，或实现编码中逐帧收集并在发现不支持特征时终止、不发布。最终输出完整校验先保留。
