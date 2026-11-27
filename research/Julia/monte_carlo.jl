using Random
using Statistics

function n8_monte_carlo(returns::Vector{Float64}; simulations::Int=10_000, trades::Int=length(returns), seed::Int=9108)
    isempty(returns) && throw(ArgumentError("returns cannot be empty"))
    any(x -> !isfinite(x) || x <= -1.0, returns) && throw(ArgumentError("invalid trade return"))
    simulations < 100 && throw(ArgumentError("at least 100 simulations required"))
    trades < 1 && throw(ArgumentError("trades must be positive"))
    rng = MersenneTwister(seed)
    drawdowns = Vector{Float64}(undef, simulations)
    for i in 1:simulations
        equity, peak, worst = 1.0, 1.0, 0.0
        for _ in 1:trades
            equity *= 1.0 + rand(rng, returns)
            peak = max(peak, equity)
            worst = max(worst, (peak-equity)/peak)
        end
        drawdowns[i] = 100.0 * worst
    end
    return (probability_ruin=mean(drawdowns .>= 50.0),
            median_drawdown_pct=quantile(drawdowns,0.50),
            p95_drawdown_pct=quantile(drawdowns,0.95))
end

if abspath(PROGRAM_FILE) == @__FILE__
    result = n8_monte_carlo([-0.10,0.04,0.08]; simulations=500, seed=12)
    @assert result.p95_drawdown_pct >= result.median_drawdown_pct
    println("9N8 Julia Monte Carlo research: PASS")
end
