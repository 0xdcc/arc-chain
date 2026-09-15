# Resume instance reload coverage

Independent review: /tmp/arc-resume-restart-candidate/REVIEW.md. Only the reviewed test insertion was integrated; original assertions preserved. Same-wallet positive remaining budget is checked after reopening SQLite via new Python objects, followed by rejection of 0.5 and admission of 0.2. This is in-process instance reload, not an OS reboot or process-crash test. Integration evidence: /tmp/arc-resume-restart-integrate/; 60 joint and 16 manifest tests passed. No new collection-error resolution claimed.
