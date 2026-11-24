#!/usr/bin/env python3
"""Patch MT7621 clock driver for CPU overclock.

Usage: patch_clk.py <path-to-clk-mt7621.c> <fbdiv>
  fbdiv 54 -> 1100 MHz
  fbdiv 49 -> 1000 MHz
"""
import sys

path = sys.argv[1]
fbdiv = int(sys.argv[2]) if len(sys.argv) > 2 else 54

s = open(path, encoding="utf-8").read()

func = r'''
static void mt7621_cpu_oc(struct mt7621_clk_priv *priv)
{
	u32 pll;

	/* Select PLL as CPU clock source */
	regmap_update_bits(priv->sysc, SYSC_REG_CLKCFG0,
			   CPU_CLK_SEL_MASK,
			   FIELD_PREP(CPU_CLK_SEL_MASK, 1));

	/*
	 * 40 MHz XTAL, prediv = 1 (/2).
	 * fbdiv 54 -> (54 + 1) * 40 / 2 = 1100 MHz
	 * fbdiv 49 -> (49 + 1) * 40 / 2 = 1000 MHz
	 */
	regmap_read(priv->memc, MEMC_REG_CPU_PLL, &pll);
	pll &= ~(CPU_PLL_FBDIV_MASK | CPU_PLL_PREDIV_MASK);
	pll |= FIELD_PREP(CPU_PLL_FBDIV_MASK, %d);
	pll |= FIELD_PREP(CPU_PLL_PREDIV_MASK, 1);
	regmap_write(priv->memc, MEMC_REG_CPU_PLL, pll);
}

''' % fbdiv

marker = 'static void __init mt7621_clk_init(struct device_node *node)\n'
assert marker in s, "init marker not found"
s = s.replace(marker, func + marker, 1)

old = '''	if (IS_ERR(priv->memc)) {
		pr_err("Could not get memc syscon regmap\\n");
		goto free_clk_priv;
	}

	count = ARRAY_SIZE(mt7621_clks_base) +'''
new = '''	if (IS_ERR(priv->memc)) {
		pr_err("Could not get memc syscon regmap\\n");
		goto free_clk_priv;
	}

	mt7621_cpu_oc(priv);

	count = ARRAY_SIZE(mt7621_clks_base) +'''
assert old in s, "call site not found"
s = s.replace(old, new, 1)

open(path, "w", encoding="utf-8").write(s)
print("kernel clock patched:", path, "fbdiv =", fbdiv)
