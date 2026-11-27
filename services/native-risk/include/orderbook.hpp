#pragma once
#include <vector>
namespace n9n8 {
struct Level{double price;double quantity;};
struct BookMetrics{double midpoint;double spread_bps;double imbalance;};
BookMetrics analyze_book(const std::vector<Level>& bids,const std::vector<Level>& asks);
double estimate_market_impact_bps(const std::vector<Level>& side,double order_quantity,bool buy);
}
