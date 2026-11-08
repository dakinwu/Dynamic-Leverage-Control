# Keep trading authority deterministic and broker-neutral

Trading authority will live in versioned deterministic Strategy Policies followed by an independent Risk Gate, while broker access remains behind interchangeable Execution Adapters. Codex or another language model may investigate, operate the platform, and propose a new policy version, but it may not invent or submit an out-of-policy discretionary trade; this sacrifices ad-hoc flexibility in exchange for replayability, auditability, bounded authority, and safe broker replacement.
