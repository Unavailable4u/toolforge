from toolforge import errors


def test_every_error_has_a_stable_code_and_hint() -> None:
    err = errors.GateFailed("G3 failed", hint="assert on returned values")
    assert err.code == "gate_failed" and err.hint and "assert on returned values" in str(err)
    for cls in (
        errors.ConfigError,
        errors.ValidationError,
        errors.SandboxUnavailable,
        errors.RegistryError,
        errors.IllegalTransition,
        errors.GateFailed,
    ):
        assert issubclass(cls, errors.ToolForgeError) and cls("x").code != "error"
