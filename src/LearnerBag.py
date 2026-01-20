import numpy as np
from scipy import stats
import pdb

class BagLearner(object):
    def __init__(self, verbose=False, learner=None, bags=10, boost=False, kwargs={'leaf_size': 5}, split=0.6):
        """
        Constructor for BagLearner.

        :param verbose: If “verbose” is True, print debugging output.
        :param learner: The learner class to be bagged or boosted.
        :param bags: The number of learners to create.
        :param boost: If True, implement AdaBoost; otherwise, implement bagging.
        :param kwargs: Keyword arguments to pass to the learner constructor.
        :param split: Fraction of data to use for bagging (ignored if boost=True).
        """
        self.bags = bags
        self.learner = learner
        self.verbose = verbose
        self.boost = boost
        self.kwargs = kwargs
        self.split = split # Only used for bagging

        # Ensemble stores learners for bagging, or (learner, alpha) tuples for boosting
        self.ensemble = []
        # Alphas store learner weights specifically for boosting
        self.alphas = []

    def author(self):
        return 'bwang421'

    def study_group(self):
        return 'bwang421'

    def random_slice(self, x_data, y_data, split_pct=0.6):
        """
        Creates a random subset of the data using bootstrap sampling (sampling with replacement).
        Used only for bagging (boost=False).
        """
        n_samples = x_data.shape[0]
        indices = np.random.choice(n_samples, int(n_samples * split_pct), replace=True)
        return x_data[indices], y_data[indices]

    def add_evidence(self, x_data, y_data):
        """
        Train the ensemble of learners.

        :param x_data: Features numpy array.
        :param y_data: Target values numpy array.
        """
        self.ensemble = [] # Reset ensemble
        n_samples = x_data.shape[0]

        if not self.boost:
            # --- Standard Bagging ---
            for _ in range(self.bags):
                new_learner = self.learner(**self.kwargs)
                x_subset, y_subset = self.random_slice(x_data, y_data, self.split)
                new_learner.add_evidence(x_subset, y_subset)
                self.ensemble.append(new_learner) # Store only the learner

        else:
            # --- AdaBoost Implementation ---
            self.alphas = [] # Reset alphas for boosting
            # Initialize sample weights uniformly
            sample_weights = np.ones(n_samples) / n_samples

            for t in range(self.bags):
                if self.verbose:
                    print(f"  Boosting iteration {t + 1}/{self.bags}")

                # ** 1. Weighted Resampling **
                # Create a new dataset for this iteration by sampling based on current weights
                indices = np.random.choice(n_samples, n_samples, replace=True, p=sample_weights)
                x_resampled, y_resampled = x_data[indices], y_data[indices]

                # ** 2. Train Weak Learner **
                weak_learner = self.learner(**self.kwargs)
                weak_learner.add_evidence(x_resampled, y_resampled)

                # ** 3. Calculate Weighted Error **
                # Predict on the *original* full dataset
                y_pred = weak_learner.query(x_data)
                # Identify misclassified samples
                misclassified_mask = y_pred != y_data
                # Sum the weights of misclassified samples
                epsilon_t = np.sum(sample_weights[misclassified_mask])

                # Add a small constant to prevent division by zero or log(0)
                epsilon_t = np.clip(epsilon_t, 1e-10, 1 - 1e-10) # Ensure epsilon is in (0, 1)

                # ** 4. Calculate Learner Weight (Alpha) **
                alpha_t = 0.5 * np.log((1.0 - epsilon_t) / epsilon_t)

                # ** 5. Update Sample Weights **
                # Increase weight for misclassified, decrease for correctly classified
                # Use exp(alpha) for wrong, exp(-alpha) for right
                # Note: This works directly, avoiding issues with y=0 if y_pred is also 0
                update_factor = np.ones(n_samples)
                update_factor[misclassified_mask] = np.exp(alpha_t)  # Increase weight
                update_factor[~misclassified_mask] = np.exp(-alpha_t) # Decrease weight

                sample_weights = sample_weights * update_factor

                # ** 6. Normalize Sample Weights **
                #breakpoint()
                sample_weights = sample_weights / np.sum(sample_weights)

                # ** 7. Store Learner and Alpha **
                self.ensemble.append(weak_learner)
                self.alphas.append(alpha_t)

                if self.verbose:
                    print(f"    Learner {t+1}: Epsilon = {epsilon_t:.4f}, Alpha = {alpha_t:.4f}")

                # Optional: Break early if error is too high or too low
                # if epsilon_t >= 0.5: # Worse than random guessing (or exactly random)
                #    print(f"    Warning: Epsilon >= 0.5, stopping early or consider handling.")
                     # Decide whether to stop or handle (e.g., discard last learner)
                #    break
                # if epsilon_t == 0: # Perfect learner
                #     print(f"    Perfect learner found, stopping early.")
                     # Can potentially set alpha to a large value and stop
                #    break


    def query(self, x_points):
        """
        Query the ensemble for predictions.

        :param x_points: Features numpy array to predict on.
        :return: Predicted values numpy array.
        """
        n_points = x_points.shape[0]

        if not self.boost:
            # --- Bagging Prediction (Mode) ---
            # Collect predictions from all learners
            predictions = np.array([learner.query(x_points) for learner in self.ensemble])
            # Return the mode (most frequent prediction) for each point
            mode_result, _ = stats.mode(predictions, axis=0) #keepdims=False) # Use keepdims=False for scipy >= 1.9.0
            return mode_result.flatten() # Ensure it's a 1D array

        else:
            # --- Boosting Prediction (Weighted Voting) ---
            # Initialize array to store the weighted sum of votes
            weighted_votes = np.zeros(n_points)

            # Ensure we have alphas stored; otherwise, something went wrong in training
            if len(self.alphas) != len(self.ensemble):
                 raise ValueError("Number of alphas does not match number of learners in boosting mode.")

            # Sum the votes weighted by alpha
            for learner, alpha in zip(self.ensemble, self.alphas):
                pred = learner.query(x_points)
                # Ensure pred is numeric, handle potential non-numeric if learner fails
                try:
                    weighted_votes += alpha * pred.astype(float)
                except ValueError:
                     print("Warning: Non-numeric prediction encountered from a base learner.")
                     # Handle appropriately, e.g., skip this learner's vote for this point
                     pass


            # Final prediction is the sign of the weighted sum
            # np.sign returns -1 for negative, 0 for zero, 1 for positive
            final_prediction = np.sign(weighted_votes)
            return final_prediction.astype(int) # Return as integers