# prompts/

Versioned prompt text, one file per agent role.

`prompt_version` is written to every event in the ledger. That is what makes
the eval harness meaningful: when you change a prompt, you can measure whether
it helped, because every past run recorded which version produced it.

Never edit a prompt without bumping the version.
