#include "orderbook.hpp"
#include <algorithm>
#include <cmath>
#include <stdexcept>
namespace n9n8 {
static void validate(const std::vector<Level>& levels){
 if(levels.empty())throw std::invalid_argument("empty order book side");
 for(const auto& x:levels)if(!std::isfinite(x.price)||!std::isfinite(x.quantity)||x.price<=0||x.quantity<=0)throw std::invalid_argument("invalid level");
}
BookMetrics analyze_book(const std::vector<Level>& bids,const std::vector<Level>& asks){
 validate(bids);validate(asks);
 const auto bid=std::max_element(bids.begin(),bids.end(),[](auto a,auto b){return a.price<b.price;})->price;
 const auto ask=std::min_element(asks.begin(),asks.end(),[](auto a,auto b){return a.price<b.price;})->price;
 if(ask<=bid)throw std::invalid_argument("crossed or locked order book");
 double bd=0,ad=0;for(const auto& x:bids)bd+=x.quantity;for(const auto& x:asks)ad+=x.quantity;
 const double mid=(bid+ask)/2;
 return {mid,(ask-bid)/mid*10000,(bd-ad)/(bd+ad)};
}
double estimate_market_impact_bps(const std::vector<Level>& side,double qty,bool buy){
 validate(side);if(!std::isfinite(qty)||qty<=0)throw std::invalid_argument("invalid quantity");
 auto levels=side;std::sort(levels.begin(),levels.end(),[buy](auto a,auto b){return buy?a.price<b.price:a.price>b.price;});
 double rem=qty,notional=0,filled=0;
 for(const auto& x:levels){double take=std::min(rem,x.quantity);notional+=take*x.price;filled+=take;rem-=take;if(rem<=1e-12)break;}
 if(rem>1e-12)throw std::runtime_error("insufficient visible depth");
 return std::abs((notional/filled-levels.front().price)/levels.front().price)*10000;
}
}
