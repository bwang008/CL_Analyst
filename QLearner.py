import random
import numpy as np
import pdb
import matplotlib.pyplot as plt
import time
from collections import deque

class QLearner(object):

    def __init__(self, num_states=100, num_actions=4, alpha=0.2, gamma=0.9, rar=0.6, radr=0.999, dyna=0, verbose=False):
        self.verbose = verbose
        self.state = 0
        self.action = 0
        self.num_states = num_states
        self.num_actions = num_actions
        self.alpha = alpha #learning rate 
        self.gamma = gamma #discount rate, how much to discount future reward values
        self.rar = rar #Chance for random action
        self.radr = radr #Random action decay
        self.dyna = dyna
        self.iteration_count = 0
        self.starttime = 0
        self.endtime = 0
        self.epoch = 0

        self.q_table = [[0.0 for a in range(num_actions)] for s in range(num_states)] #create list of list (100 rows x4 cols)
        #Try to make q_table array...
        #self.q_table = np.zeros([100,4])
        self.transition = {} #Dictionary which stores the state/action as keys to assign to next state for dyna
        self.reward = {}

        self.experience_buffer = []  # Experience Replay Buffer

    def author(self):
        return 'bwang421'

    def querysetstate(self, s):
        self.state = s #Assign the robot starting position
        
        if random.random() <= self.rar: #If we take a random action
            action = random.randint(0, self.num_actions - 1)
        else:
            action = self.q_table[s].index(max(self.q_table[s]))    
            #action = np.argmax(self.q_table[s])

        self.action = action
        #if self.verbose:
        #    print(f" querysetstate: s = {s}, a = {action} radr: {self.radr} alpha:{self.alpha} gamma:{self.gamma}")
        return action
    
    #def update(self,state,action,reward,s_prime):
    #    self.q_table[state][action]=(1 - self.alpha) * self.q_table[state][action]\
    #    + self.alpha * (reward + self.gamma * max(self.q_table[s_prime]))

    def query(self, s_prime, r):
        
        #Update the q_table value
        self.iteration_count+=1 #Internal counter for debugging

        #Q(s,a) = (1-learn_rate)*Q(s,a)+learn_rate*(reward+discount_rate*(Action with highest Q value at s_prime))

        #Update the current state with information about the next state
        #Update q value, where q value is the mean reward of the action at the given state.
        #r is the reward given for taking the action
        self.q_table[self.state][self.action] = (1 - self.alpha) * \
            self.q_table[self.state][self.action] + self.alpha *\
               (r + self.gamma * max(self.q_table[s_prime]))
        #self.update(self.state,self.action,r,s_prime)

        start_time = time.perf_counter()
        if self.dyna > 0: #implement dyna
            
            ###Store experience in buffer for random sampling with probability for experience replay:
            self.experience_buffer.append((self.state, self.action, s_prime, r))


            if (self.state, self.action) not in self.transition: 
                #If the current state hasn't been reviewed before then add it to the list of stored experiences
                #Key will be state,action and value will be recommended action?
                self.transition[(self.state, self.action)] = s_prime
                self.reward[(self.state, self.action)] = r

            self.epoch+=1
            for _ in range(self.dyna): #hallucinate X number of times (dyna)
                if self.experience_buffer: #experience replay
                    s_rand, a_rand, s_prime_rand, r_rand = random.choice(self.experience_buffer)
                else: #Transition table
                    #Pick a random experience from the list of experiences
                    s_rand, a_rand = random.choice(list(self.transition.keys()))
                    #s_rand = random.randint(0,self.num_actions-1)
                    #a_rand = random.randint(0,self.num_states-1)
                    #recall the previous s_prime and reward
                    s_prime_rand = self.transition[(s_rand, a_rand)]
                    r_rand = self.reward[(s_rand, a_rand)]
                    
                
                #if self.verbose == True:
                #    print("  Before Update", self.q_table[s_rand][a_rand])
                    #pdb.set_trace()
                
                #update the Q table to reinforce the values
                
                #Q(s,a) = Q(s,a) + alpha*[r + gamma*max(Q(s',a)) - Q(s,a)]

                #self.q_table[s_rand][a_rand] = (1 - self.alpha) * self.q_table[s_rand][a_rand] + self.alpha*(r_rand + self.gamma * max(self.q_table[s_prime_rand]))

                #The equation is fine, it's something else that is causing issues.
                #self.q_table[s_rand][a_rand] = (1 - self.alpha) * self.q_table[s_rand][a_rand]+\
                #    self.alpha*(r_rand + self.gamma * max(self.q_table[s_prime_rand]))

                #alternative, maybe faster?
                self.q_table[s_rand][a_rand] = self.q_table[s_rand][a_rand]\
                    + self.alpha*(r_rand + self.gamma*max(self.q_table[s_prime_rand]) - self.q_table[s_rand][a_rand])

                #Going to try to use other method..
                #Q'(s,a)= Q(s,a)+alpha*(new_X - Q(s,a))
                #self.q_table[s_rand][a_rand] = self.q_table[s_rand][a_rand] + self.alpha*(r_rand - self.q_table[s_rand][a_rand])
            end_time = time.perf_counter()

            if self.verbose == True:
                if self.epoch % 1000 == 0:
                    print(f"Epoch:{self.epoch}   Time to dyna:", end_time -start_time)


        #Decay the random stepping to eventually stabalize/converge.
        self.rar *= self.radr

        if self.verbose == True and self.iteration_count % 1000 == 0:
            print(f"self.rar = {self.rar} iteration: {self.iteration_count}")
        
        action = random.randint(0, self.num_actions - 1)
        if random.random() > self.rar:
            action = self.q_table[s_prime].index(max(self.q_table[s_prime]))
            
            #action = self.pickbest(s_prime) #pick random tie instead of first one
            #action = np.argmax(self.q_table[s_prime])

        #if self.verbose:
        #    print(f"query: s = {self.state}, a = {self.action}, s_prime = {s_prime}, r = {r}, action = {action}")

        self.state = s_prime
        self.action = action
        #pdb.set_trace()
        return action
    
    def pickbest(self,s_prime):
        q_values = self.q_table[s_prime]
        max_q = max(q_values)
        max_indices = [i for i, value in enumerate(q_values) if value == max_q]
        return random.choice(max_indices)