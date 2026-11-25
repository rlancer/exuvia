-- Latest 20d vol and trailing 1-month return, averaged by sector.
-- key: sector
-- chart: {"mark": "bar", "encoding": {"x": {"field": "sector", "type": "nominal", "sort": "-y"}, "y": {"field": "avg_vol", "type": "quantitative", "axis": {"format": ".0%"}}}}
with latest as (
    select ticker, vol_20d
    from rolling_vol
    qualify row_number() over (partition by ticker order by date desc) = 1
),
month as (
    select ticker, sum(ret) as ret_1m
    from returns
    where date >= (select max(date) from returns) - interval 30 day
    group by ticker
)
select u.sector,
       count(*)              as tickers,
       avg(l.vol_20d)        as avg_vol,
       avg(m.ret_1m)         as avg_ret_1m
from universe u
join latest l using (ticker)
join month m using (ticker)
group by u.sector
order by avg_vol desc
