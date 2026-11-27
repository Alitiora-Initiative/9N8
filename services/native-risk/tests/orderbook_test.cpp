#include "orderbook.hpp"
#include <cassert>
#include <cmath>
#include <stdexcept>
int main(){
 auto m=n9n8::analyze_book({{100,5},{99,3}},{{101,2},{102,2}});
 assert(std::abs(m.midpoint-100.5)<1e-9);assert(m.spread_bps>0);assert(m.imbalance>0);
 assert(n9n8::estimate_market_impact_bps({{101,1},{102,3}},2,true)>0);
 bool rejected=false;try{(void)n9n8::estimate_market_impact_bps({{101,1}},3,true);}catch(const std::runtime_error&){rejected=true;}
 assert(rejected);return 0;
}
