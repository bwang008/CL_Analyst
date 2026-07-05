# Ticket Audit Log — `parallel-batch-vm-conflict_07042026_1955`

[2026-07-04 19:56:00] | parallel-batch-vm-conflict_07042026_1955 | TICKET-AUDITOR | Began RCA investigation of hardcoded VM name conflict in parallel batch execution across gcp_deploy_optimizer.ps1, vm_post_optimize.sh, and run_sweep_batch.ps1.

[2026-07-04 19:57:00] | parallel-batch-vm-conflict_07042026_1955 | TICKET-AUDITOR | Confirmed root cause: gcp_deploy_optimizer.ps1:L16 hardcodes `$VmName = "optuna-post-optimizer"` and run_sweep_batch.ps1:L983 hardcodes `$optVmName = "optuna-post-optimizer"`. Both are shared by all concurrent batch orchestrators. Identified 6 additional parallel-unsafe patterns across all three scripts.

[2026-07-04 19:58:00] | parallel-batch-vm-conflict_07042026_1955 | TICKET-AUDITOR | Classified severity as HIGH. Proposed fix: parameterize all batch-scoped identifiers (VM name, tmux session, on-VM project directory) using the BatchId that is already threaded through the pipeline, requiring changes in 3 files.

[2026-07-04 19:59:00] | parallel-batch-vm-conflict_07042026_1955 | TICKET-IMPACT-REVIEWER | Blast-radius review: CONDITIONAL APPROVAL. Fix is sound for run_sweep_batch.ps1 and gcp_deploy_optimizer.ps1. However, scope is INCOMPLETE — run_canary_batch.ps1:L647-L691 also hardcodes `$optVmName = "optuna-post-optimizer"` (L658) and does NOT pass `-VmName` to gcp_deploy_optimizer.ps1 (L647-L653). Must apply identical parameterization to run_canary_batch.ps1 before merging. No Interface/Base-Class/Refactor-Veto rules triggered — changes are mechanical parameterization within one subsystem. Documentation refs (docs/prompts/run_local_optimizer_prompt.md:L77-78, workflows/build-symbol-pipeline.md:L118) contain hardcoded name but are non-functional and can be updated separately.
