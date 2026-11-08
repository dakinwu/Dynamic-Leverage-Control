# Separate exposure policy from execution recipe

Strategy Policies will output only Target Exposure, while independently versioned Execution Recipes map that target to instruments. The platform will compare both leveraged-ETF-plus-cash and 1x-plus-2x recipes instead of treating equal initial exposure as equal economic behavior, allowing costs, path dependence, liquidity, and product abnormality to be measured without contaminating the strategy signal.
