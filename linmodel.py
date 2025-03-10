"""
Implementation of Linear Programming controller class for CityLearn model.

LinProgModel class is used to construct, hold, and solve LP models of the
CityLearn environment for use in a Linear MPC controller.
"""

from citylearn.citylearn import CityLearnEnv
import numpy as np
import cvxpy as cp
from pathlib import Path
from typing import Any, List, Dict, Mapping, Tuple, Union


class LinProgModel():

    def __init__(self, schema: Union[str, Path] = None , env: CityLearnEnv = None) -> None:
        """Set up CityLearn environment from provided schema, and collected required data.

        Note: it is assumed all data is clean and appropriately formatted.

        Args:
            schema (Union[str, Path]): path to schema.json defining model setup
            env (CityLearnEnv): pre-constructred environment object to use
        """

        if schema is not None and env is not None:
            raise ValueError("Cannot provide both a schema and a CityLearnEnv object.")

        if schema is not None:
            self.env = CityLearnEnv(schema)
        else:
            self.env = env

        self.b_names = [b.name for b in self.env.buildings]
        self.Tmax = self.env.time_steps # number of timesteps available
        self.delta_t = self.env.seconds_per_time_step/3600


    def set_battery_propery_data(self, b_inds: List[int] = None):
        """Set battery property data from CityLearnEnv object.
        Note: efficiency and power capacity curves are neglected

        Args:
            b_inds (List[int], optional): indices of buildings to grab data for (relative to self.env.buildings).
                If no indices are provided, all buildings are used.
        """
        # get battery property specifications from schema.json

        if b_inds is not None: self.b_inds = b_inds
        else: self.b_inds = range(len(self.b_names))

        self.buildings = [self.env.buildings[ind] for ind in self.b_inds]

        self.battery_efficiencies = np.array([b.electrical_storage.efficiency\
            for b in self.buildings])
        self.battery_loss_coeffs = np.array([b.electrical_storage.loss_coefficient\
            for b in self.buildings])
        self.battery_max_powers = np.array([b.electrical_storage.available_nominal_power\
            for b in self.buildings])
        self.battery_capacities = np.array([b.electrical_storage.capacity\
            for b in self.buildings])


    def set_time_data_from_env(self, tau: int = None, t_start: int = None,
        current_socs: np.array = None) -> None:
        """Set time variant data for model from data given by CityLearnEnv object for period
        [t_start+1, t_start+tau] (inclusive).

        Note: this corresponds to using perfect data for the prediction model of the system
        in a state at time t, with planning horizon tau. `current_socs` values are the
        state-of-charge at the batteries at the beginning of time period t.

        Args:
            tau (int, optional): number of time instances included in LP model. Defaults to None.
            t_start (int, optional): starting time index for LP model. Defaults to None.
            current_socs (np.array, optional): initial states of charge of batteries in
                period before t_start (kWh). Defaults to None.
        """
        # useful for investigating how the time horizon of perfect information MPC affects performance
        # (compare to full information global LP performance) - in some ways this is VoI-ish as it
        # can give the value of extending the perfect forecasting horizon

        if not self.buildings:
            raise NameError("Battery data must be contructed before providing time data.")

        if not t_start: self.t_start = 0
        else: self.t_start = t_start

        if not tau: self.tau = (self.Tmax - 1) - self.t_start
        else:
            if tau > (self.Tmax - 1) - self.t_start: raise ValueError("`tau` cannot be greater than remaining time instances, (Tmax - 1) - t_start.")
            else: self.tau = tau

        # initialise battery state for period before t_start
        if current_socs is not None:
            self.battery_initial_socs = current_socs
        else: # note this will default to zero if not specified in schema
            self.battery_initial_socs = np.array([b.electrical_storage.initial_soc\
                for b in self.buildings])

        self.elec_loads = np.array(
            [b.energy_simulation.non_shiftable_load[self.t_start+1:self.t_start+self.tau+1]\
                for b in self.buildings])
        self.solar_gens = np.array(
            [b.pv.get_generation(b.energy_simulation.solar_generation)[self.t_start+1:self.t_start+self.tau+1]\
                for b in self.buildings])
        self.prices = np.array(
            self.buildings[0].pricing.electricity_pricing[self.t_start+1:self.t_start+self.tau+1])
        self.carbon_intensities = np.array(
            self.buildings[0].carbon_intensity.carbon_intensity[self.t_start+1:self.t_start+self.tau+1])


    def set_custom_time_data(self, elec_loads: np.array, solar_gens: np.array, prices: np.array,
        carbon_intensities: np.array, current_socs: np.array = None) -> None:
        """Set custom time variant data for model.

        This is used to load in forecast/prediction data in the LP model of the system for Linear MPC.

        Note: for a model prediction for the system in a state at time t, with planning horizon tau,
        the load, solar generation, pricing, and carbon intensity prediction values are for the period
        [t+1, t+tau], and the `current_socs` values are the state-of-charge values of the batteries at
        the start of time t.

        Args:
            elec_loads (np.array): electrical loads of buildings in each period (kWh) - shape (N,tau)
            solar_gens (np.array): energy generations of pv panels in each period (kWh) - shape (N,tau)
            prices (np.array): grid electricity price in each period ($/kWh) - shape (tau)
            carbon_intensities (np.array): grid electricity carbon intensity in each period (kgCO2/kWh) - shape (tau)
            current_socs (np.array, optional): initial states of charge of batteries in
                period before t_start (kWh). Defaults to None.
        """

        if not hasattr(self,'buildings'): raise NameError("Battery data must be contructed before providing time data.")

        assert elec_loads.shape[0] == solar_gens.shape[0] == len(self.buildings),\
            "Data must be provided for all buildings used in model."
        assert elec_loads.shape[1] == solar_gens.shape[1] == prices.shape[0] == carbon_intensities.shape[0],\
            "Data provided must have consistent time duration."

        if not hasattr(self,'tau'):
            self.tau = elec_loads.shape[1]
            assert self.tau > 0, "Must provide at least one period of data"
        else:
            assert elec_loads.shape[1] == self.tau, "Predicted time series must have length equal to specified planning horizon, tau."

        # initialise battery state for period before t_start
        if current_socs is not None:
            self.battery_initial_socs = current_socs
        else: # note this will default to zero if not specified in schema
            self.battery_initial_socs = np.array([b.electrical_storage.initial_soc\
                for b in self.buildings])

        self.elec_loads = elec_loads
        self.solar_gens = solar_gens
        self.prices = prices
        self.carbon_intensities = carbon_intensities


    def generate_LP(self, objective_dict: Dict[str,bool] = {'price':True, 'carbon':True, 'ramping':True},
        clip_level: str = 'd') -> None:
        """Set up CVXPY LP of CityLearn model with data specified by schema, for
        desired buildings over specified time period.

        Note: we need to be extremely careful about the time indexing of the different variables (decision and data),
        see comments in implementation for details.

        Args:
            objective_dict (Dict[str,bool or float] ,optional): dictionary indicating contribution weightings
            in overall objective of LP. keys: objective contributions, values: either bools indicating
            whether to use the contribution in an even weighting, or an explicit normalised float weighting.
            clip_level (Str, optional): str, either 'd' (district) or 'b' (building), indicating
            the level at which to clip cost values in the objective function
        """

        if not hasattr(self,'buildings'): raise NameError("Building properties must be set before LP can be generated.")
        if not hasattr(self,'tau'): raise NameError("Planning horizon must be set before LP can be generated.")

        assert all([type(val) in [bool,float] for val in objective_dict.values()])
        # weightings must sum to one unless all are Bools, at which point an even weighting is applied to contributions with value 'True'
        if all([type(val) == bool for val in objective_dict.values()]):
            assert True in list(objective_dict.values()), "Objective cannot be empty, `objective_dict` must contain at least one 'True' entry."
        else:
            np.testing.assert_approx_equal(np.sum(list(objective_dict.values())), 1.0, significant=3, err_msg="Objective contributions do not sum to 1.")

        self.objective_dict = objective_dict.copy()

        if True in list(self.objective_dict.values()): # if Boolean weightings given
            n_trues = list(self.objective_dict.values()).count(True)
            for key,val in self.objective_dict.items():
                if val == True:
                    self.objective_dict[key] = 1/n_trues # set True contributions as evenly weighted

        assert clip_level in ['d','b'], "`clip_level` value must be either 'd' (district) or 'b' (building)."

        self.N = len(self.buildings)
        assert self.N > 0


        # initialise decision variables
        self.SoC = cp.Variable(shape=(self.N,self.tau), nonneg=True) # for [t+1,t+tau]
        self.alpha = cp.Variable(shape=(self.N,self.tau)) # for [t,t+tau-1]

        # initialise problem parameters
        self.current_socs = cp.Parameter(shape=(self.N))
        self.elec_loads_param = cp.Parameter(shape=(self.N,self.tau))
        self.solar_gens_param = cp.Parameter(shape=(self.N,self.tau))
        self.prices_param = cp.Parameter(shape=(self.tau))
        self.carbon_intensities_param = cp.Parameter(shape=(self.tau))


        # compute no storage objective values - for [t+1,t+tau]
        self.e_grids_without = cp.sum(self.elec_loads_param - self.solar_gens_param, axis=0)
        self.ramp_without = cp.norm(self.e_grids_without[1:]-self.e_grids_without[:-1],1)
        if clip_level == 'd':
            # aggregate costs at district level (CityLearn <= 1.6 objective)
            # costs are computed from clipped e_grids value - i.e. looking at portfolio elec. cost
            self.price_without = cp.pos(self.e_grids_without) @ self.prices_param
            self.carbon_without = cp.pos(self.e_grids_without) @ self.carbon_intensities_param
        elif clip_level == 'b':
            # aggregate costs at building level and average (CityLearn >= 1.7 objective)
            # costs are computed from clipped building power flow values - i.e. looking at mean building elec. cost
            self.building_power_flows_without = self.elec_loads_param - self.solar_gens_param
            self.price_without = cp.sum(cp.pos(self.building_power_flows_without), axis=0) @ self.prices_param
            self.carbon_without = cp.sum(cp.pos(self.building_power_flows_without), axis=0) @ self.carbon_intensities_param


        # set up constraints
        self.constraints = []

        # initial storage dynamics constraint - for t=0
        self.constraints += [self.SoC[:,0] <= self.current_socs +\
            cp.multiply(\
                cp.multiply(self.alpha[:,0],self.battery_capacities),\
                    np.sqrt(self.battery_efficiencies))]
        self.constraints += [self.SoC[:,0] <= self.current_socs +\
            cp.multiply(\
                cp.multiply(self.alpha[:,0],self.battery_capacities),\
                    1/np.sqrt(self.battery_efficiencies))]

        # storage dynamics constraints - for t \in [t+1,t+tau-1]
        self.constraints += [self.SoC[:,1:] <= self.SoC[:,:-1] +\
            cp.multiply(\
                cp.multiply(self.alpha[:,1:],np.tile(self.battery_capacities.reshape(self.N,1),self.tau-1)),\
                    np.tile((np.sqrt(self.battery_efficiencies)).reshape(self.N,1),self.tau-1))]
        self.constraints += [self.SoC[:,1:] <= self.SoC[:,:-1] +\
            cp.multiply(\
                cp.multiply(self.alpha[:,1:],np.tile(self.battery_capacities.reshape(self.N,1),self.tau-1)),\
                    np.tile((1/np.sqrt(self.battery_efficiencies)).reshape(self.N,1),self.tau-1))]

        # storage power constraints - for t \in [t,t+tau-1]
        self.constraints += [-1*np.tile(self.battery_max_powers.reshape(self.N,1),self.tau)*self.delta_t <=\
            cp.multiply(self.alpha,np.tile(self.battery_capacities.reshape(self.N,1),self.tau))]
        self.constraints += [cp.multiply(self.alpha,np.tile(self.battery_capacities.reshape(self.N,1),self.tau)) <=\
            np.tile(self.battery_max_powers.reshape(self.N,1),self.tau)*self.delta_t]

        # storage energy constraints - for t \in [t+1,t+tau]
        self.constraints += [self.SoC <= np.tile(self.battery_capacities.reshape(self.N,1),self.tau)]


        # define objective
        self.e_grids = cp.sum(self.elec_loads_param - self.solar_gens_param +\
            cp.multiply(self.alpha,np.tile(self.battery_capacities.reshape(self.N,1),self.tau)),\
                axis=0) # for [t+1,t+tau]

        objective_contributions = []

        if objective_dict['price'] or objective_dict['carbon']:

            if clip_level == 'd':
                # aggregate costs at district level (CityLearn <= 1.6 objective)
                # costs are computed from clipped e_grids value - i.e. looking at portfolio elec. cost
                self.xi = cp.Variable(self.tau, nonneg=True)
                self.constraints += [self.xi >= self.e_grids] # for t \in [t+1,t+tau]

                if objective_dict['price']:
                    objective_contributions.append((self.xi @ self.prices_param)/cp.maximum(self.price_without,1))
                if objective_dict['carbon']:
                    objective_contributions.append((self.xi @ self.carbon_intensities_param)/cp.maximum(self.carbon_without,1))

            elif clip_level == 'b':
                # aggregate costs at building level and average (CityLearn >= 1.7 objective)
                # costs are computed from clipped building power flow values - i.e. looking at mean building elec. cost
                self.bxi = cp.Variable(shape=(self.N,self.tau), nonneg=True) # building level xi
                self.building_power_flows = self.elec_loads_param - self.solar_gens_param +\
                    cp.multiply(self.alpha,np.tile(self.battery_capacities.reshape(self.N,1),self.tau))
                self.constraints += [self.bxi >= self.building_power_flows] # for t \in [t+1,t+tau]

                if objective_dict['price']:
                    objective_contributions.append((cp.sum(self.bxi, axis=0) @ self.prices_param)/cp.maximum(self.price_without,1))
                if objective_dict['carbon']:
                    objective_contributions.append((cp.sum(self.bxi, axis=0) @ self.carbon_intensities_param)/cp.maximum(self.carbon_without,1))

        if objective_dict['ramping']:
            objective_contributions.append(cp.norm(self.e_grids[1:]-self.e_grids[:-1],1)/cp.maximum(self.ramp_without,1))

        objective_contributions = cp.hstack(objective_contributions) # convert to 1d cvxpy array
        obj_weights = np.array([self.objective_dict[key] for key in ['price','carbon','ramping'] if self.objective_dict[key]]) # enforces ordering and removes nulls

        self.obj = objective_contributions @ obj_weights

        self.objective = cp.Minimize(self.obj)


        # construct problem
        self.problem = cp.Problem(self.objective,self.constraints)


    def set_LP_parameters(self):
        """Set value of CVXPY parameters using loaded data."""

        if not hasattr(self,'problem'): raise NameError("LP must be generated before parameters can be set.")
        if not hasattr(self,'elec_loads') or not hasattr(self,'solar_gens') or not hasattr(self,'prices')\
            or not hasattr(self,'carbon_intensities') or not hasattr(self,'battery_initial_socs'):
            raise NameError("Data must be loaded before parameters can be set.")

        # NOTE: clip parameter values at 0 to prevent LP solve issues
        # This requirement is for the current LP formulation and could be
        # relaxed with an alternative model setup.
        self.current_socs.value = self.battery_initial_socs.clip(min=0)
        self.elec_loads_param.value = self.elec_loads.clip(min=0)
        self.solar_gens_param.value = self.solar_gens.clip(min=0)
        self.prices_param.value = self.prices.clip(min=0)
        self.carbon_intensities_param.value = self.carbon_intensities.clip(min=0)


    def solve_LP(self, **kwargs):
        """Solve LP model of specified problem.

        Args:
            **kwargs: optional keyword arguments for solver settings.

        Returns:
            self.objective.value (float): optimised objective value.
            objective_breakdown (List[float]): breakdown of objective contributions (price, carbon, ramping).
            obj_check (float): optimised objective value computed using original inputs for checking.
            self.SoC.value (np.array[float]): optimised states of charge of batteries.
            self.alpha.value (np.array[float]): optimised fractional battery control actions.
        """

        if not hasattr(self,'problem'): raise ValueError("LP model has not been generated.")

        if 'solver' not in kwargs: kwargs['solver'] = 'SCIPY'
        if 'verbose' not in kwargs: kwargs['verbose'] = False
        if kwargs['solver'] == 'SCIPY': kwargs['scipy_options'] = {'method':'highs'}
        if kwargs['verbose'] == True: kwargs['scipy_options'].update({'disp':True})

        try:
            self.problem.solve(**kwargs)
        except cp.error.SolverError:
            print("Current SoCs: ", self.current_socs.value)
            print("Building loads:", self.elec_loads_param.value)
            print("Solar generations: ", self.solar_gens_param.value)
            print("Pricing: ", self.prices_param.value)
            print("Carbon intensities: ", self.carbon_intensities_param.value)
            raise Exception("LP solver failed. Check your forecasts. Try solving in verbose mode. If issue persists please contact organizers.")


        # prep results
        self.price_contr = np.maximum(self.e_grids.value,0) @ self.prices if self.objective_dict['price'] else np.NaN
        self.carbon_contr = np.maximum(self.e_grids.value,0) @ self.carbon_intensities if self.objective_dict['carbon'] else np.NaN
        self.ramp_contr = np.sum(np.abs(self.e_grids.value[1:]-self.e_grids.value[:-1])) if self.objective_dict['ramping'] else np.NaN
        objective_breakdown = np.array([
            self.price_contr/np.maximum(self.price_without.value,1),
            self.carbon_contr/np.maximum(self.carbon_without.value,1),
            self.ramp_contr/np.maximum(self.ramp_without.value,1)
            ])
        obj_weights = np.array([self.objective_dict[key] for key in ['price','carbon','ramping'] if self.objective_dict[key]]) # enforces ordering and removes nulls
        obj_check = objective_breakdown[~np.isnan(objective_breakdown)] @ obj_weights[~np.isnan(objective_breakdown)]

        return self.objective.value, objective_breakdown, obj_check, self.SoC.value, self.alpha.value


    def get_LP_data(self, solver: str, **kwargs):
        """Get LP problem data used in CVXPY call to specified solver,
        as specified in https://www.cvxpy.org/api_reference/cvxpy.problems.html#cvxpy.Problem.get_problem_data

        Args:
            solver (str): desired solver.
            kwargs (dict): keywords arguments for cvxpy.Problem.get_problem_data().

        Returns:
            solver_data: data passed to solver in solve call, as specified in link to docs above.
        """

        if not hasattr(self,'problem'): raise NameError("LP model has not been generated.")

        return self.problem.get_problem_data(solver, **kwargs)