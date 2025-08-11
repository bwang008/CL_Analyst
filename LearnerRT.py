""""""                   
"""                   
A simple wrapper for linear regression.  (c) 2015 Tucker Balch                   
                   
Copyright 2018, Georgia Institute of Technology (Georgia Tech)                   
Atlanta, Georgia 30332                   
All Rights Reserved                   
                   
Template code for CS 4646/7646                   
                   
Georgia Tech asserts copyright ownership of this template and all derivative                   
works, including solutions to the projects assigned in this course. Students                   
and other users of this template code are advised not to share it with others                   
or to make it available on publicly viewable websites including repositories                   
such as github and gitlab.  This copyright statement should not be removed                   
or edited.                   
                   
We do grant permission to share solutions privately with non-students such                   
as potential employers. However, sharing with other current or future                   
students of CS 7646 is prohibited and subject to being investigated as a                   
GT honor code violation.                   
                   
-----do not edit anything above this line---                   
"""                   
                   
import numpy as np  
import pdb
from scipy import stats
#import random                 
                   
                   
class RTLearner(object):                   
    #do the same thing as DT learner but pick a random feature instead of calculating correlation

    #This is a copy of the DTLearner code, but I changed the find_corr function to find_rand

    def __init__(self, leaf_size=1, verbose=False):                   
        """                   
        Constructor method                   
        """    
        self.leaf_size = leaf_size
        self.verbose = verbose       
        self.decision_tree = None  
        self.leaf = int(-1) 
        self.dbg=False    
        pass  # move along, these aren't the drones you're looking for                   

    def talk(self, string):
        if self.verbose == True:
            print(string)

    def author(self):                   
        """                   
        :return: The GT username of the student                   
        :rtype: str                   
        """                   
        return "bwang421"  # replace tb34 with your Georgia Tech username    

    def printm(self, msg):
        if self.verbose == True:
            print(msg)
        return None
    
    def find_rand(self, x_data, y_data):
        """
        Selects a random column from a 2D numpy array.
        """

        if not isinstance(x_data, np.ndarray) or x_data.ndim != 2 or x_data.size == 0:  # Check for valid input
            return None

        num_cols = x_data.shape[1]  # Get the number of columns
        if num_cols == 0:  # Check if the array has any columns.
            return None

        random_column_index = np.random.randint(0, num_cols)  # Generate a random index
        return random_column_index


    def build_tree(self, x_data, y_data):
        leaf = -1
        x = x_data
        y = y_data

        leaf_size = self.leaf_size


        #termination cases
        if x.shape[0] <= leaf_size:
            self.talk("Single Sample. End.")
            #return np.array([leaf,np.median(y),None, None])
            #return np.array([leaf,np.mean(y),None, None])
            return np.array([leaf,stats.mode(y)[0][0],None,None])


        
        elif np.all(y == y[0]):
            #compare first row with the rest. If they're all the same then kill.
            self.talk("Same Sample. End.")
            #return np.array([leaf, np.median(y),None,None])
            #return np.array([leaf,np.mean(y),None, None])
            return np.array([leaf,stats.mode(y)[0][0],None,None])
        else:  
            #did not terminate so let's build the tree
            '''
            Algorithm:
            [1] determine best feature i to split on - calculate correlation np.corrcoef
            [2] SplitVal = data[:,i].median()
            [3] lefttree = build_tree(data[data[:,i]<=SplitVal])
                righttree = build_tree(data[data[:,i]>SplitVal])
                root = [i, SplitVal, 1, lefttree.shape[0] + 1]
            [4] return (append(root, lefttree, righttree))
            '''

            #[1][2] 

            split_val = self.find_rand(x,y)

            #We got split column, so find the median within the split column
            median = np.median(x[:,split_val])
            
            #Now use the median value to create a boolean mask of X data
            #split_val = column number , median is value to split on (misleading var names.)
            mask = x[:,split_val] <= median

            ##Add a condition here to check if the mask contains the same value. If so, then we know the other array/tree will be empty
            #if that is the case, then terminate and return the 1st record of the batch and assign it as a leaf.
            #print("  split_val, median", split_val, median)
            #if np.all(mask==mask[0]):
            #    return np.array([leaf, np.median(y), None, None])

            if y[mask].shape[0] == 0 or y[~mask].shape[0] == 0:
                return np.array([leaf, stats.mode(y)[0][0], None, None])

            if self.dbg==True:
                self.talk("Right before calling build_tree: Line 99")

            left_tree = self.build_tree(x[mask],y[mask])
            right_tree = self.build_tree(x[~mask],y[~mask])

            #root is i, splitval, 1, lefttree.shape[0]+1
            
            self.talk(" Build Root")

            #split_val - column number, median is value to split on, 1 is start of right tree, and the last is start of left tree.
            if len(left_tree.shape) == 1:
                lt_shape=1
            else:
                lt_shape = left_tree.shape[0]
            
            root = np.array([int(split_val),float(median), int(1),int(lt_shape+1) if left_tree is not None else 0], dtype=object) #remove hard code for later.

            #[4] return back the entire tree

            #print("    Vstacking")
            dt_model = np.vstack((root, left_tree, right_tree))
            if self.dbg==True:
                pdb.set_trace()

            #pdb.set_trace()
            return dt_model

    def add_evidence(self, data_x, data_y):
        #create a decision tree from this data
        x_rows = data_x.shape[0]
        x_cols = data_x.shape[1]
        y_shape = data_y.shape[0]

        if x_rows == y_shape:
            self.decision_tree = self.build_tree(data_x, data_y)
        elif x_cols == y_shape:
            #inverted so transpose
            self.decision_tree = self.build_tree(data_x.T, data_y)
        #pdb.set_trace()
        return self.decision_tree
     
    def query(self, x_data):
        #given x_data features, predict y
        #we have access to the model as self.model, so with self.model, how do we want to predict each row?
        if self.decision_tree is None:
            return print("No model trained. Terminating program.")
        
        '''
        [1] step through each sample in x_data
        [2] with each sample, step through the tree until you reach a leaf node
        [3] terminate the search and assign the leaf node value as the prediction
        [4] once you are done with all x_data, return the predictions as vector
        '''
        y_pred = np.zeros(x_data.shape[0]) #array to store the predictions
        tree = self.decision_tree
        #[1]
        #i vector: feature, split value, left tree, right tree
        #i vector for leaf: -1, assign value, -1, -1

        for idx,x in enumerate(x_data):
            feature_col = 0


            #[2] step through the tree
            node_index = 0 #initialize to root node
            while feature_col != -1:
                feature_col = tree[node_index,0] 
                feature_val = tree[node_index,1]
                left_tree = tree[node_index,2]
                right_tree = tree[node_index,3]

                if feature_col == -1:
                    skip=True #we're done so let the loop terminate
                elif x[feature_col] <= feature_val:
                    node_index+=left_tree
                else:
                    node_index+=right_tree

            #we exit while loop, this means that the leaf should be found unless something is wrong.
            if feature_col == -1:
                y_pred[idx] = feature_val
            else:
                print("   Something is wrong. Could not find a leaf value. Check x_data and")   

        return y_pred


if __name__ == "__main__":                   
    print("the secret clue is 'zzyzx'")                   
