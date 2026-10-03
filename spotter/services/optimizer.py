from dataclasses import dataclass

from requests.packages import target

EPS = 1e-9 # tolerance for "is this float effectively zero?"

class NoReachableStation(Exception):
    def __init__(self, mile_marker: float):
        super().__init__(f"No reachable fuel station beyond mile {mile_marker:.0f}")
        self.mile_marker = mile_marker
    
@dataclass(frozen=True)
class Purchase:
    node: int
    mile_marker: float
    price: float
    gallons: float

    @property
    def cost(self) -> float:
        return self.gallons * self.price

def plan_fuel(miles, prices, *, max_range_miles=500.0, mpg=10.0, start_fuel_gallons=0.0):
    # decide where to buy fuel and how much in order to cover the whole route as cheaply as possible
    
    if len(miles) != len(prices) or len(miles) < 2:
        raise ValueError("miles & prices must have same length and atleast 2 nodes")
    
    if any(b < a for a,b in zip(miles, miles[1:])):
        raise ValueError("nodes must be sorted by mile marker")

    tank_gallons = max_range_miles / mpg
    n = len(miles)
    i, fuel, purchases = 0, start_fuel_gallons, []

    while i < n - 1:
        reach = miles[i] + max_range_miles + EPS
        cheaper = best = None

        j = i + 1
        
        while j < n and miles[j] <= reach:
            if prices[j] < prices[i]:
                cheaper = j
                break
            
            if best is None or prices[j] <= prices[best]:
                best = j
            
            j += 1
        
        if cheaper is not None:
            target = cheaper
            needed = (miles[target] - miles[i]) / mpg
            buy = max(0.0, needed - fuel)
        elif best is not None:
            target = best
            buy = tank_gallons - fuel
        else:
            raise NoReachableStation(miles[i])
        
        if buy > EPS:
            purchases.append(Purchase(i, miles[i], prices[i], buy))
        
        fuel += buy - (miles[target] - miles[i]) / mpg
        i = target
    
    return purchases