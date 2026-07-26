# Security

For the current alpha, report security-sensitive issues through GitHub private vulnerability reporting. Please include a minimal reproduction and remove keys, private images, prompts, and provider responses.

`OPENAI_API_KEY` is read from the process environment. If it is exposed, rotate it with the provider. Path handling, package integrity, and unexpected provider requests are in scope; ordinary product feedback belongs in an issue.
