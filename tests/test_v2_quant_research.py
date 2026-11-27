from decimal import Decimal
import pytest
from src.n9n8.v2.quant_research import geometric_brownian_path,mean_reversion_half_life,sample_autocorrelation,scalar_kalman_filter,walk_forward_splits

def test_autocorrelation_and_kalman():
    assert sample_autocorrelation([1,2,3,4,5,6],lag=1)>0
    result=scalar_kalman_filter([100,102,101,103,102],process_variance=0.001,measurement_variance=0.1)
    assert len(result)==5 and min(result)>=100 and max(result)<=103

def test_invalid_kalman_variance_rejected():
    with pytest.raises(ValueError,match="variances"):
        scalar_kalman_filter([1,2],measurement_variance=0)

def test_gbm_and_walk_forward_are_reproducible_and_chronological():
    a=geometric_brownian_path("100","0.05","0.2",steps=12,seed=42)
    b=geometric_brownian_path("100","0.05","0.2",steps=12,seed=42)
    assert a==b and a[0]==Decimal("100")
    windows=walk_forward_splits(100,train_size=50,test_size=10,step_size=10)
    assert windows[0][0].stop==windows[0][1].start
    assert windows[-1][1].stop<=100

def test_mean_reversion_half_life():
    assert mean_reversion_half_life([10,8,9,7,8,6,7,5,6,4,5,3])>0
    with pytest.raises(ValueError,match="not mean reverting"):
        mean_reversion_half_life([1,2,3,4,5,6])
