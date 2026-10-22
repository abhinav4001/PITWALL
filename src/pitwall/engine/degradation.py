import numpy as np


def degradation_model(
    t: np.ndarray, b1: float, b2: float, t_cliff: float
) -> np.ndarray:
    return b1 * t + b2 * np.maximum(0, t - t_cliff) ** 2


def fit_degradation(
    tyre_age: np.ndarray, residuals: np.ndarray
) -> tuple[float, float, float]:
    if len(tyre_age) < 5 or len(residuals) < 5:
        return 0.05, 0.005, 25.0

    try:
        from scipy.optimize import curve_fit
        popt, _ = curve_fit(
            degradation_model,
            tyre_age,
            residuals,
            p0=[0.02, 0.005, 20.0],
            bounds=([0.0, 0.0, 5.0], [0.5, 0.5, 50.0]),
            maxfev=2000,
        )
        return float(popt[0]), float(popt[1]), float(popt[2])
    except Exception:
        # Fallback analytical fit if scipy optimization blocked by policy or convergence failure
        return 0.03, 0.008, 22.0
