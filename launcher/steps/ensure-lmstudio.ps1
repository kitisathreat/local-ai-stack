param([string]$RepoRoot)
. (Join-Path $PSScriptRoot "_common.ps1")

# Master's architecture uses Ollama (containerized) as the primary inference
# backend, with llama.cpp as the vision tier. LM Studio is no longer part of
# the launch path, so this step is now a no-op. Kept as a placeholder in case
# a future profile re-enables an LM Studio backend.
Emit-Result -Ok $true -Message "Inference handled by Ollama container — no host-side action needed"
