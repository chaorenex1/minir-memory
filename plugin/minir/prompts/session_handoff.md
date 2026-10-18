# Task Handoff

  ## Goal

  [最终要实现的用户结果，而不是当前正在做的动作]

  ## Scope

  ### Included

  [本次明确包含的功能、模块、环境]

  ### Excluded

  [明确不处理的内容，防止后续扩大范围]

  ## Acceptance Criteria

  - [ ] [可验证的行为或结果]
  - [ ] [测试、兼容性或性能要求]
  - [ ] [部署后必须成立的条件]

  ## Constraints & Preferences

  - [语言、风格、兼容性、安全要求]
  - [禁止修改的文件、API 或行为]
  - [是否允许新增依赖、迁移、重启、部署]
  - [用户已有改动必须保留]

  ## Current State

  [系统目前实际行为，以及它与目标状态的差距]

  ## Progress

  ### Done

  - [完成事项]
      - Files: [path]
      - Verification: [命令及结果]

  ### In Progress

  - [当前事项]
  - Last completed operation: [最近成功完成的动作]
  - Resume from: [恢复工作时的准确起点]

  ### Pending

  - [尚未开始但仍属于范围内的事项]

  ### Blocked

  - Blocker: [具体阻塞条件]
  - Impact: [阻塞了什么]
  - Attempts: [已经尝试过的方法]
  - Unblock condition: [需要什么变化才能继续]

  ## Key Decisions

   Decision    Rationale    Alternatives Rejected    Impact
  ━━━━━━━━━━  ━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━
   [决定]      [原因]       [替代方案及拒绝原因]     [影响范围]

  ## Assumptions & Unknowns

  ### Confirmed

  - [已有代码、日志或文档证据支持的事实]

  ### Assumptions

  - [暂时采用但未完全验证的假设]

  ### Open Questions

  - [只有用户或外部系统才能回答的问题]

  ## Relevant Files

   Path      Status                       Purpose
  ━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━
   [path]    read / modified / created    [用途及关键符号]

  ## Changes Made

  - [path]: [具体修改及行为影响]
  - API/schema/config changes: [契约变化]
  - Compatibility impact: [兼容性影响]

  ## Worktree State

  - Branch: [branch]
  - HEAD: [commit]
  - Staged changes: [有/无，具体范围]
  - Unstaged changes: [有/无，具体范围]
  - Untracked files: [有/无，具体范围]
  - Pre-existing user changes: [必须保留的改动]

  ## Verification Evidence

   Check              Command      Result
   ━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━
   Unit tests         [command]    [通过数量或失败详情]
   ─────────────────  ───────────  ──────────────────────
   Typecheck          [command]    [结果]
   ─────────────────  ───────────  ──────────────────────
   Lint               [command]    [结果]
   ─────────────────  ───────────  ──────────────────────
   Integration/E2E    [command]    [结果]
   ─────────────────  ───────────  ──────────────────────
   Manual smoke       [步骤]       [结果]

  ## Known Failures & Risks

  - [仍存在的问题、严重度和影响]
  - [未覆盖的边界条件]
  - [可能出现回归的区域]
  - [测试通过但尚不能证明的事项]

  ## Runtime & Deployment State

  - Source modified: [是/否]
  - Commit created: [commit 或否]
  - Deployed/installed: [是/否及目标路径]
  - Migration applied: [是/否]
  - Service restarted: [是/否]
  - Source/install parity verified: [是/否]
  - Live smoke verified: [是/否]
  - Rollback method: [回滚方式]

  ## Commands & Artifacts

  ### Important Commands

  [可安全重跑的命令]

  ### Generated Artifacts
  - `[path]`: [测试报告、日志、计划、备份等]

  ### Relevant Logs
  - `[path]`
  - Time range: `[时间范围和时区]`
  - Correlation IDs: `[request/job/session IDs]`

  ## Next Steps
  1. [下一步准确动作]
  2. [对应验证方式]
  3. [完成条件]

  ## Stop Condition
  [满足哪些条件后任务才算真正完成]

  ## Critical Context
  - Environment: `[OS/runtime/version]`
  - Repository root: `[absolute path]`
  - Important IDs/ports/schema versions: `[values]`
  - Exact errors:
    ```text
    [完整错误信息]
    ```

  - Do not repeat: [已证明无效或有风险的操作]
