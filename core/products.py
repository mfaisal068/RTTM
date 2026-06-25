"""
RTTM Product Library v2.0
Fluid property presets for hydrocarbon liquids, pure fluids, and gas mixtures.
Properties at standard reference conditions; temperature corrections applied in solver.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ProductCategory(str, Enum):
    HYDROCARBON = "hydrocarbon"
    PURE_FLUID  = "pure_fluid"
    GAS_MIXTURE = "gas_mixture"


@dataclass
class FluidProduct:
    name: str
    category: ProductCategory
    # Liquid properties
    density: float          # kg/m³ at ref conditions
    viscosity: float        # Pa·s dynamic
    bulk_modulus: float     # Pa (liquids) / 0 for gas
    specific_heat: float    # J/(kg·K)
    thermal_expansion: float  # 1/K (volume)
    # Gas-specific
    z_factor: float = 0.0
    molar_mass: float = 0.0  # kg/mol
    gamma: float = 0.0       # Cp/Cv
    # Reference conditions
    ref_temperature: float = 288.15   # K (15°C)
    ref_pressure: float    = 101325.0  # Pa

    def density_at(self, T: float, P: float) -> float:
        """Density corrected for temperature and pressure."""
        if self.category == ProductCategory.GAS_MIXTURE:
            # Ideal gas with Z correction: ρ = PM/(ZRT)
            R = 8.314
            return (P * self.molar_mass) / (self.z_factor * R * T)
        else:
            # Liquid: ρ(T,P) = ρ₀ · [1 − β(T−T₀)] · [1 + (P−P₀)/K]
            rho = self.density
            rho *= (1.0 - self.thermal_expansion * (T - self.ref_temperature))
            rho *= (1.0 + (P - self.ref_pressure) / self.bulk_modulus)
            return max(rho, 1.0)

    def viscosity_at(self, T: float) -> float:
        """Andrade equation viscosity correction: μ(T) = μ₀·exp(B(1/T−1/T₀))."""
        if self.category == ProductCategory.GAS_MIXTURE:
            # Sutherland: μ ∝ T^1.5 / (T + S)
            S = 110.4
            T0 = self.ref_temperature
            return self.viscosity * (T/T0)**1.5 * (T0 + S) / (T + S)
        else:
            B = 1500.0  # Typical Andrade constant for petroleum
            T0 = self.ref_temperature
            return self.viscosity * float(__import__('math').exp(B * (1/T - 1/T0)))


# ─── Product Registry ─────────────────────────────────────────────────────────

PRODUCTS: dict[str, dict[str, FluidProduct]] = {

    "hydrocarbon": {
        "Crude Oil (Arabian Light)": FluidProduct(
            name="Crude Oil (Arabian Light)", category=ProductCategory.HYDROCARBON,
            density=855, viscosity=0.012, bulk_modulus=1.72e9,
            specific_heat=2100, thermal_expansion=7.2e-4,
        ),
        "Crude Oil (Heavy)": FluidProduct(
            name="Crude Oil (Heavy)", category=ProductCategory.HYDROCARBON,
            density=920, viscosity=0.08, bulk_modulus=1.90e9,
            specific_heat=1950, thermal_expansion=6.5e-4,
        ),
        "Diesel / Gas Oil": FluidProduct(
            name="Diesel / Gas Oil", category=ProductCategory.HYDROCARBON,
            density=832, viscosity=0.0028, bulk_modulus=1.55e9,
            specific_heat=2170, thermal_expansion=8.5e-4,
        ),
        "Gasoline (Motor Spirit)": FluidProduct(
            name="Gasoline (Motor Spirit)", category=ProductCategory.HYDROCARBON,
            density=740, viscosity=0.00055, bulk_modulus=1.18e9,
            specific_heat=2220, thermal_expansion=9.5e-4,
        ),
        "Condensate / NGL": FluidProduct(
            name="Condensate / NGL", category=ProductCategory.HYDROCARBON,
            density=780, viscosity=0.0018, bulk_modulus=1.38e9,
            specific_heat=2050, thermal_expansion=9.0e-4,
        ),
        "Jet Fuel (Avtur)": FluidProduct(
            name="Jet Fuel (Avtur)", category=ProductCategory.HYDROCARBON,
            density=800, viscosity=0.0019, bulk_modulus=1.45e9,
            specific_heat=2100, thermal_expansion=8.8e-4,
        ),
    },

    "pure_fluid": {
        "Water": FluidProduct(
            name="Water", category=ProductCategory.PURE_FLUID,
            density=998, viscosity=0.001, bulk_modulus=2.18e9,
            specific_heat=4182, thermal_expansion=2.1e-4,
        ),
        "Kerosene": FluidProduct(
            name="Kerosene", category=ProductCategory.PURE_FLUID,
            density=800, viscosity=0.0016, bulk_modulus=1.42e9,
            specific_heat=2090, thermal_expansion=8.6e-4,
        ),
        "Methanol": FluidProduct(
            name="Methanol", category=ProductCategory.PURE_FLUID,
            density=791, viscosity=0.00059, bulk_modulus=8.3e8,
            specific_heat=2530, thermal_expansion=1.2e-3,
        ),
        "Ethanol": FluidProduct(
            name="Ethanol", category=ProductCategory.PURE_FLUID,
            density=789, viscosity=0.0011, bulk_modulus=8.9e8,
            specific_heat=2440, thermal_expansion=1.1e-3,
        ),
        "LPG (Propane)": FluidProduct(
            name="LPG (Propane)", category=ProductCategory.PURE_FLUID,
            density=500, viscosity=0.00011, bulk_modulus=4.3e8,
            specific_heat=2500, thermal_expansion=2.0e-3,
        ),
    },

    "gas_mixture": {
        "Natural Gas (Methane-rich)": FluidProduct(
            name="Natural Gas (Methane-rich)", category=ProductCategory.GAS_MIXTURE,
            density=0.72, viscosity=1.1e-5, bulk_modulus=0,
            specific_heat=2220, thermal_expansion=0,
            z_factor=0.92, molar_mass=0.01704, gamma=1.31,
        ),
        "Natural Gas (Wet)": FluidProduct(
            name="Natural Gas (Wet)", category=ProductCategory.GAS_MIXTURE,
            density=0.85, viscosity=1.2e-5, bulk_modulus=0,
            specific_heat=2100, thermal_expansion=0,
            z_factor=0.88, molar_mass=0.0195, gamma=1.27,
        ),
        "CO₂": FluidProduct(
            name="CO₂", category=ProductCategory.GAS_MIXTURE,
            density=1.87, viscosity=1.48e-5, bulk_modulus=0,
            specific_heat=850, thermal_expansion=0,
            z_factor=0.94, molar_mass=0.04401, gamma=1.29,
        ),
        "Hydrogen (H₂)": FluidProduct(
            name="Hydrogen (H₂)", category=ProductCategory.GAS_MIXTURE,
            density=0.089, viscosity=8.8e-6, bulk_modulus=0,
            specific_heat=14300, thermal_expansion=0,
            z_factor=1.00, molar_mass=0.00202, gamma=1.41,
        ),
        "Sour Gas (H₂S blend)": FluidProduct(
            name="Sour Gas (H₂S blend)", category=ProductCategory.GAS_MIXTURE,
            density=1.10, viscosity=1.3e-5, bulk_modulus=0,
            specific_heat=1550, thermal_expansion=0,
            z_factor=0.85, molar_mass=0.0260, gamma=1.25,
        ),
    },
}


def get_product(category: str, name: str) -> Optional[FluidProduct]:
    return PRODUCTS.get(category, {}).get(name)


def list_products() -> dict:
    return {cat: list(prods.keys()) for cat, prods in PRODUCTS.items()}
