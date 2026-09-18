# 深度实现与进阶避坑（Deep Dive & FAQ）

本目录面向技术专家、AI Coding Agent 以及需要深入定制网关能力的架构师，涵盖协议线级规范、前端 React 无缝对接与高并发企业级实战经验：

1. **[01. 前端 React 0 改动集成与 DevTools 调试面板](01-react-zero-code-integration.md)**：针对已有 AG-UI / SSE 前端 harness 的平滑升级指南与完整开发面板使用。
2. **[02. AG-UI 线级通信协议规范 (Wire Protocol Spec)](02-protocol-wire-spec.md)**：严格规范每一个事件帧（`RUN_STARTED`, `TEXT`, `TOOL_CALL`, `CUSTOM run.paused`）的物理结构与状态机生命周期。
3. **[03. 生产避坑指南与高频 FAQ 全景 (Production Pitfalls & FAQ)](03-production-pitfalls-and-faq.md)**：IM 平台限流、鉴权安全、Dual Storage、PostgreSQL 原生 JSONB 检索、Chime-in 最小权限策略与核心常见问答。
