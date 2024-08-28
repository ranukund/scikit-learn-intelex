# ==============================================================================
# Copyright 2023 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from abc import ABCMeta, abstractmethod
from numbers import Number

import numpy as np

from daal4py.sklearn._utils import daal_check_version, get_dtype, make2d

from ..common._base import BaseEstimator as onedal_BaseEstimator
from ..common._estimator_checks import _check_is_fitted
from ..common._mixin import ClassifierMixin
from ..datatypes import _convert_to_supported, from_table, to_table
from ..utils import (
    _check_array,
    _check_n_features,
    _check_X_y,
    _is_csr,
    _num_features,
    _type_of_target,
)


class BaseLogisticRegression(onedal_BaseEstimator, metaclass=ABCMeta):
    @abstractmethod
    def __init__(self, tol, C, fit_intercept, solver, max_iter, algorithm):
        self.tol = tol
        self.C = C
        self.fit_intercept = fit_intercept
        self.solver = solver
        self.max_iter = max_iter
        self.algorithm = algorithm

    def _get_onedal_params(self, is_csr, dtype=np.float32):
        intercept = "intercept|" if self.fit_intercept else ""
        return {
            "fptype": "float" if dtype == np.float32 else "double",
            "method": "sparse" if is_csr else self.algorithm,
            "intercept": self.fit_intercept,
            "tol": self.tol,
            "max_iter": self.max_iter,
            "C": self.C,
            "optimizer": self.solver,
            "result_option": (
                intercept
                + "coefficients|iterations_count"
                + ("|inner_iterations_count" if self.solver == "newton-cg" else "")
            ),
        }

    def _fit(self, X, y, module, queue):
        sparsity_enabled = daal_check_version((2024, "P", 700))
        X, y = _check_X_y(
            X,
            y,
            accept_sparse=sparsity_enabled,
            force_all_finite=True,
            accept_2d_y=False,
            dtype=[np.float64, np.float32],
        )
        is_csr = _is_csr(X)

        self.n_features_in_ = _num_features(X, fallback_1d=True)

        if _type_of_target(y) != "binary":
            raise ValueError("Only binary classification is supported")

        self.classes_, y = np.unique(y, return_inverse=True)
        y = y.astype(dtype=np.int32)

        policy = self._get_policy(queue, X, y)
        X, y = _convert_to_supported(policy, X, y)
        params = self._get_onedal_params(is_csr, get_dtype(X))
        X_table, y_table = to_table(X, y)

        result = module.train(policy, params, X_table, y_table)

        self._onedal_model = result.model
        self.n_iter_ = np.array([result.iterations_count])

        # _n_inner_iter is the total number of cg-solver iterations
        if daal_check_version((2024, "P", 300)) and self.solver == "newton-cg":
            self._n_inner_iter = result.inner_iterations_count

        coeff = from_table(result.model.packed_coefficients)
        self.coef_, self.intercept_ = coeff[:, 1:], coeff[:, 0]

        return self

    def _create_model(self, module, policy):
        m = module.model()

        coefficients = self.coef_
        dtype = get_dtype(coefficients)
        coefficients = np.asarray(coefficients, dtype=dtype)

        if coefficients.ndim == 2:
            n_features_in = coefficients.shape[1]
            assert coefficients.shape[0] == 1
        else:
            n_features_in = coefficients.size

        intercept = self.intercept_
        if not isinstance(intercept, Number):
            intercept = np.asarray(intercept, dtype=dtype)
            assert intercept.size == 1

        intercept = _check_array(
            intercept,
            dtype=[np.float64, np.float32],
            force_all_finite=True,
            ensure_2d=False,
        )
        coefficients = _check_array(
            coefficients,
            dtype=[np.float64, np.float32],
            force_all_finite=True,
            ensure_2d=False,
        )

        coefficients, intercept = make2d(coefficients), make2d(intercept)

        assert coefficients.shape == (1, n_features_in)
        assert intercept.shape == (1, 1)

        desired_shape = (1, n_features_in + 1)
        packed_coefficients = np.zeros(desired_shape, dtype=dtype)

        packed_coefficients[:, 1:] = coefficients
        if self.fit_intercept:
            packed_coefficients[:, 0][:, np.newaxis] = intercept

        packed_coefficients = _convert_to_supported(policy, packed_coefficients)

        m.packed_coefficients = to_table(packed_coefficients)

        self._onedal_model = m

        return m

    def _infer(self, X, module, queue):
        _check_is_fitted(self)
        sparsity_enabled = daal_check_version((2024, "P", 700))

        X = _check_array(
            X,
            dtype=[np.float64, np.float32],
            accept_sparse=sparsity_enabled,
            force_all_finite=True,
            ensure_2d=False,
            accept_large_sparse=sparsity_enabled,
        )
        is_csr = _is_csr(X)
        _check_n_features(self, X, False)

        X = make2d(X)
        policy = self._get_policy(queue, X)

        if hasattr(self, "_onedal_model"):
            model = self._onedal_model
        else:
            model = self._create_model(module, policy)

        X = _convert_to_supported(policy, X)
        params = self._get_onedal_params(is_csr, get_dtype(X))

        X_table = to_table(X)
        result = module.infer(policy, params, model, X_table)
        return result

    def _predict(self, X, module, queue):
        result = self._infer(X, module, queue)
        y = from_table(result.responses)
        y = np.take(self.classes_, y.ravel(), axis=0)
        return y

    def _predict_proba(self, X, module, queue):
        result = self._infer(X, module, queue)

        y = from_table(result.probabilities)
        y = y.reshape(-1, 1)
        return np.hstack([1 - y, y])

    def _predict_log_proba(self, X, module, queue):
        y_proba = self._predict_proba(X, module, queue)
        return np.log(y_proba)


class LogisticRegression(ClassifierMixin, BaseLogisticRegression):
    """
    Logistic Regression oneDAL implementation.
    """

    def __init__(
        self,
        tol=1e-4,
        C=1.0,
        fit_intercept=True,
        solver="newton-cg",
        max_iter=100,
        *,
        algorithm="dense_batch",
        **kwargs,
    ):
        super().__init__(
            tol=tol,
            C=C,
            fit_intercept=fit_intercept,
            solver=solver,
            max_iter=max_iter,
            algorithm=algorithm,
        )

    def fit(self, X, y, queue=None):
        return super()._fit(
            X, y, self._get_backend("logistic_regression", "classification", None), queue
        )

    def predict(self, X, queue=None):
        y = super()._predict(
            X, self._get_backend("logistic_regression", "classification", None), queue
        )
        return y

    def predict_proba(self, X, queue=None):
        y = super()._predict_proba(
            X, self._get_backend("logistic_regression", "classification", None), queue
        )
        return y

    def predict_log_proba(self, X, queue=None):
        y = super()._predict_log_proba(
            X, self._get_backend("logistic_regression", "classification", None), queue
        )
        return y
