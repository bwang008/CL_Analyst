# indicators.py
import datetime as dt
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
#import util as ut
import pdb


def author():
    return "bwang421"


def moving_average(prices, window=20):
    return prices.rolling(window=window).mean()


def rsi(prices, window=14):
    delta = prices.diff() #find p/l
    up, down = delta.copy(), delta.copy() #create 2 separate for gain and loss
    up[up < 0] = 0 #Identify all gains
    down[down > 0] = 0 #identify all losses
    roll_up = up.rolling(window=window).mean() #avg gain
    roll_down = -down.rolling(window=window).mean() #avg loss
    rs = roll_up / roll_down
    return 100.0 - (100.0 / (1.0 + rs))


def momentum(prices, window=10):
    return (prices / prices.shift(window)) - 1


def stochastic_oscillator(prices, window=20,plot=False):
    low = prices.rolling(window=window).min()
    high = prices.rolling(window=window).max()
    k = 100 * (prices - low) / (high - low)
    d = k.rolling(window=5).mean()

    if plot==True:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10), sharex=True)
        # Top Subplot: Price with Bollinger Bands
        ax1.plot(prices.index, prices, label="Price JPM", color="navy")
        #pdb.set_trace()
        ax1.yaxis.set_major_formatter('${x:1.2f}')
        ax1.plot(low.index, low, label="High", color="green", linestyle="--")
        ax1.plot(high.index, high, label="Low", color="red", linestyle="--")
        
        ax1.set_ylabel("Price (Adjusted Close)")
        ax1.set_title("Price (Adjusted Close) with Stochastic Oscillator (14 days)")
        ax1.legend(loc="lower left")
        ax1.grid(True)

        # Bottom Subplot: %B with Overbought and Oversold Lines
        ax2.plot(k.index, k, label="%K (20)", color="black")
        ax2.plot(d.index,d,label="%D (5)",color="blue")
        ax2.axhline(y=80, color="red", linestyle="--", label="Overbought")
        ax2.axhline(y=20, color="green", linestyle="--", label="Oversold")
        ax2.set_ylabel("%K")
        ax2.set_xlabel("Date")
        ax2.set_yticks(np.linspace(0,1,11)) # Set y-ticks for better clarity

        ax2.legend(loc="upper left")
        ax2.grid(True)

        plt.tight_layout() # Adjust layout to prevent overlapping
        plt.savefig('Stochastic Oscillator.png')
        plt.close()

    if plot == False:
        return k
    else:
        return low, high, k


def plot_indicator(prices, indicator, indicator_name, window=20):
    plt.figure(figsize=(12, 6))
    plt.plot(prices, label="Price", color="black")
    plt.plot(indicator, label=indicator_name, color="blue")
    plt.title(f"{indicator_name} for JPM")
    plt.xlabel("Date")
    plt.ylabel("Price / Indicator Value")
    plt.legend()
    plt.grid(True)
    plt.savefig(f"{indicator_name}.png")
    plt.close()


def bollinger_bands(prices, window=20,plot=False):
    sma = prices.rolling(window=window).mean()
    std = prices.rolling(window=window).std()
    upper_band = sma + 2 * std
    lower_band = sma - 2 * std
    bbp = (prices - lower_band) / (upper_band - lower_band)

    if plot==True:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10), sharex=True)

        # Top Subplot: Price with Bollinger Bands
        #ax1.plot(prices.index, prices, label="Price JPM", color="navy")
        #pdb.set_trace()
        ax1.yaxis.set_major_formatter('${x:1.2f}')
        ax1.plot(sma.index, sma, label="SMA", color="gray")
        ax1.plot(upper_band.index, upper_band, label="Upper Band", color="green", linestyle="--")
        ax1.plot(lower_band.index, lower_band, label="Lower Band", color="red", linestyle="--")

        #Fill the area between the SMA and Bands for a better visual effect
        ax1.fill_between(upper_band.index, upper_band, lower_band, color="lightblue", alpha=0.2) # Shaded area
        
        ax1.set_ylabel("Price (Adjusted Close)")
        ax1.set_title("Price (Adjusted Close) with Bollinger Bands and %B for JPM (20 Day)")
        ax1.legend(loc="lower left")
        ax1.grid(True)

        # Bottom Subplot: %B with Overbought and Oversold Lines
        ax2.plot(bbp.index, bbp, label="%B", color="black")
        ax2.axhline(y=0.8, color="red", linestyle="--", label="Overbought")
        ax2.axhline(y=0.2, color="green", linestyle="--", label="Oversold")
        ax2.set_ylabel("%B")
        ax2.set_xlabel("Date")
        ax2.set_yticks(np.linspace(0,1,11)) # Set y-ticks for better clarity

        ax2.legend(loc="upper left")
        ax2.grid(True)

        plt.tight_layout() # Adjust layout to prevent overlapping
        plt.savefig('Bollinger.png')
        plt.close()

    return bbp

def split_plot(prices,indicator,indicator_name,upper=None,lower=None):

    fig, (ax1, ax2) = plt.subplots(nrows=2, ncols=1, figsize=(16, 10)) # Create two subplots, share x-axis

    # Top Subplot: Price with Bollinger Bands
    ax1.yaxis.set_major_formatter('${x:1.2f}')
    ax1.plot(prices.index, prices, label="Price JPM", color="navy")
    ax1.set_ylabel("Price (Adjusted Close)")
    ax1.set_title(f"Price (Adjusted Close) with {indicator_name}")
    ax1.legend(loc="lower left")
    ax1.grid(True)

    # Bottom Subplot: %B with Overbought and Oversold Lines
    ax2.plot(indicator.index, indicator, label=f"{indicator_name}", color="black")
    if upper != None:
        ax2.axhline(y=upper, color="green", linestyle="--", label="Overbought")
    if lower != None:
        ax2.axhline(y=lower, color="red", linestyle="--", label="Oversold")
    
    ax2.set_ylabel(f"{indicator_name}")
    ax2.set_xlabel("Date")
    #ax2.set_yticks([0.0, 0.5, 1.0]) # Set y-ticks for better clarity
    ax2.legend(loc="upper left")
    ax2.grid(True)

    plt.tight_layout() # Adjust layout to prevent overlapping
    plt.savefig(f'{indicator_name}.png')
    plt.close()

def main():
    symbol = "JPM"

if __name__ == "__main__":
    main()