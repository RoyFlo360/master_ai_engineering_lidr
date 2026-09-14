# master_ai_engineering_lidr
AI Engineering Master con Lidr

## Running the tests

```shell
uv run pytest          # whole suite
uv run pytest tests/test_llm_service.py -v
```

The suite is hermetic: settings are built in-process (`_env_file=None`) and the
OpenAI-compatible client is replaced by a recorder, so no test reads your `.env`
or calls the network.
