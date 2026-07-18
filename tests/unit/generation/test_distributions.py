"""Tests del modelo reutilizable DistributionSpec (forma anidada, §5/§8/§11)."""

import pytest
from pydantic import ValidationError

from synthdb.generation.generators.distributions import (
    DistributionSpec,
    LognormalParams,
    NormalParams,
    UniformParams,
    ZipfParams,
)


def test_default_is_uniform():
    spec = DistributionSpec()
    assert spec.family == "uniform"
    assert isinstance(spec.params, UniformParams)


def test_parses_each_family_into_its_params_model():
    normal = DistributionSpec.model_validate({"family": "normal", "params": {"mean": 5, "std": 2}})
    assert isinstance(normal.params, NormalParams)
    assert (normal.params.mean, normal.params.std) == (5, 2)

    logn = DistributionSpec.model_validate(
        {"family": "lognormal", "params": {"median": 90, "sigma": 0.45}}
    )
    assert isinstance(logn.params, LognormalParams)

    zipf = DistributionSpec.model_validate({"family": "zipf", "params": {"s": 1.3}})
    assert isinstance(zipf.params, ZipfParams)
    assert zipf.params.s == 1.3


def test_unknown_family_is_rejected():
    with pytest.raises(ValidationError):
        DistributionSpec.model_validate({"family": "poisson", "params": {}})


def test_unknown_param_for_family_is_rejected():
    # Un campo válido para otra familia no se ignora: error exacto.
    with pytest.raises(ValidationError):
        DistributionSpec.model_validate({"family": "normal", "params": {"s": 1.2}})
    with pytest.raises(ValidationError):
        DistributionSpec.model_validate({"family": "zipf", "params": {"median": 10}})


def test_family_value_constraints():
    with pytest.raises(ValidationError):
        DistributionSpec.model_validate({"family": "normal", "params": {"std": -1}})
    with pytest.raises(ValidationError):
        DistributionSpec.model_validate({"family": "lognormal", "params": {"sigma": 0}})
    with pytest.raises(ValidationError):
        DistributionSpec.model_validate({"family": "lognormal", "params": {"median": 0}})
    with pytest.raises(ValidationError):
        DistributionSpec.model_validate({"family": "zipf", "params": {"s": 0}})


def test_extra_key_at_distribution_level_is_rejected():
    with pytest.raises(ValidationError):
        DistributionSpec.model_validate({"family": "uniform", "params": {}, "bogus": 1})


def test_missing_params_defaults_to_empty_family_params():
    # Sin 'params', la familia usa sus valores por defecto.
    spec = DistributionSpec.model_validate({"family": "zipf"})
    assert isinstance(spec.params, ZipfParams)
    assert spec.params.s == 1.2
