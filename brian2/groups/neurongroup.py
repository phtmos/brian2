import weakref

import numpy as np
from numpy import array, zeros

from brian2.equations import Equations
from brian2.stateupdaters.integration import euler
from brian2.codegen.languages import PythonLanguage
from brian2.codegen.specifiers import (Value, ArrayVariable, Subexpression,
                                       Index)
from brian2.codegen.translation import translate
from brian2.memory import allocate_array
from brian2.core.preferences import brian_prefs
from brian2.core.base import BrianObject
from brian2.utils.logger import get_logger
from brian2.groups.group import Group

__all__ = ['NeuronGroup',
           'CodeRunner']

logger = get_logger(__name__)

class CodeRunner(BrianObject):
    '''
    Runs a code object on an update schedule.
    
    Inserts the current time into the namespace at each step.
    '''
    basename = 'code_runner'
    def __init__(self, codeobj, when=None, name=None):
        BrianObject.__init__(self, when=when, name=name)
        self.codeobj = codeobj
        
    def update(self):
        self.codeobj(t=self.clock.t_)
        
        
class Thresholder(CodeRunner):
    def __init__(self, group, codeobj, when=None, name=None):
        CodeRunner.__init__(self, codeobj, when=when, name=name)
        self.group = weakref.proxy(group)

    def update(self):
        CodeRunner.update(self)
        ns = self.codeobj.namespace
        spikesarray = ns['_spikes_space']
        numspikes = ns['_array_num_spikes'][0]
        self.group.spikes = spikesarray[:numspikes]
        

class Resetter(Thresholder):
    def update(self):
        spikes = self.group.spikes
        self.codeobj.namespace['_spikes'] = spikes
        self.codeobj.namespace['_num_spikes'] = len(spikes)
        CodeRunner.update(self)
        

class NeuronGroup(BrianObject, Group):
    '''
    Group of neurons
    
    Parameters
    ----------
    N : int
        Number of neurons in the group.
    equations : (str, `Equations`)
        The differential equations defining the group
    method : ?, optional
        The numerical integration method.
    threshold : str, optional
        The condition which produces spikes. Should be a single line boolean
        expression.
    reset : str, optional
        The (possibly multi-line) string with the code to execute on reset.
    dtype : (`dtype`, `dict`), optional
        The `numpy.dtype` that will be used to store the values, or
        :bpref:`default_scalar_dtype` if not specified (`numpy.float64` by
        default).
    clock : Clock, optional
        The update clock to be used, or defaultclock if not specified.
    name : str, optional
        A unique name for the group, otherwise use ``neurongroup_0``, etc.
        
    Notes
    -----
    
    `NeuronGroup` contains a `StateUpdater`, `Thresholder` and `Resetter`, and
    these are run at the 'groups', 'thresholds' and 'resets' slots (i.e. the
    values of `Scheduler.when` take these values). The `Scheduler.order`
    attribute is set to 0 initially, but this can be modified using the
    attributes `state_updater`, `thresholder` and `resetter`.    
    '''
    basename = 'neurongroup'
    def __init__(self, N, equations, method=euler,
                 threshold=None,
                 reset=None,
                 dtype=None, language=None,
                 clock=None, name=None):
        BrianObject.__init__(self, when=clock, name=name)
        ##### VALIDATE ARGUMENTS AND STORE ATTRIBUTES
        self.method = method
        try:
            self.N = N = int(N)
        except ValueError:
            if isinstance(N, str):
                raise TypeError("First NeuronGroup argument should be size, not equations.")
            raise
        if N<1:
            raise ValueError("NeuronGroup size should be at least 1, was "+str(N))
        # Validate equations
        if isinstance(equations, basestring):
            equations = Equations(equations, level=1)
        if not isinstance(equations, Equations):
            raise ValueError(('equations has to be a string or an Equations '
                              'object, is "%s" instead.') % type(equations))
        self.equations = equations
        
        logger.debug("Creating NeuronGroup of size {self.N}, "
                     "equations {self.equations}.".format(self=self))
        
        # Check flags
        equations.check_flags({'diff_equation': ('active'),
                               'parameter': ('constant')})
        
        # Set dtypes and units
        self.prepare_dtypes(dtype=dtype)
        self.units = equations.units
        
        # Allocate memory (TODO: this should be refactored somewhere at some point)
        self.allocate_memory()

        #: The array of spikes from the most recent threshold operation
        self.spikes = array([], dtype=int)
        
        # Code generation (TODO: this should be refactored and modularised)
        # Temporary, set default language to Python
        if language is None:
            language = PythonLanguage()
        self.language = language
        self.create_state_updater()
        self.create_thresholder(threshold)
        self.create_resetter(reset)
        
        # Creation of contained_objects that do the work
        self.contained_objects.append(self.state_updater)
        if self.thresholder is not None:
            self.contained_objects.append(self.thresholder)
        if self.resetter is not None:
            self.contained_objects.append(self.resetter)
        
        # Activate name attribute access
        Group.__init__(self)
        
    def prepare_dtypes(self, dtype=None):
        # Allocate memory (TODO: this should be refactored somewhere at some point)
        arrayvarnames = set(eq.varname for eq in self.equations.equations.itervalues() if eq.eq_type in ('diff_equation', 'parameter'))
        self.dtypes = {}
        for name in arrayvarnames:
            if isinstance(dtype, dict):
                curdtype = dtype[name]
            else:
                curdtype = dtype
            if curdtype is None:
                curdtype = brian_prefs.default_scalar_dtype
            self.dtypes[name] = curdtype
        logger.debug("NeuronGroup dtypes: "+", ".join(name+'='+str(dtype) for name, dtype in self.dtypes.iteritems()))

    def allocate_memory(self, dtype=None):
        # Allocate memory (TODO: this should be refactored somewhere at some point)
        self.arrays = {}
        for name, curdtype in self.dtypes.iteritems():
            self.arrays[name] = allocate_array(self.N, dtype=curdtype)
        logger.debug("NeuronGroup memory allocated successfully.")

    def create_codeobj(self, name, abstract_code, specs, template_method,
                       additional_namespace={}):
        lang = self.language
        logger.debug("NeuronGroup "+name+" abstract code:\n"+abstract_code)
        innercode = translate(abstract_code, specs,
                              brian_prefs.default_scalar_dtype,
                              lang)
        logger.debug("NeuronGroup "+name+" inner code:\n"+str(innercode))
        code = lang.apply_template(innercode, template_method())
        logger.debug("NeuronGroup "+name+" code:\n"+str(code))
        codeobj = lang.code_object(code, specs)
        namespace = {}
        for name, arr in self.arrays.iteritems():
            namespace['_array_'+name] = arr
        if not hasattr(self, 'namespace'):
            self.namespace = namespace
        self.namespace.update(**additional_namespace)
        self.namespace['_num_neurons'] = self.N
        self.namespace['dt'] = self.clock.dt_
        self.namespace['t'] = self.clock.t_
        codeobj.compile(self.namespace)
        return codeobj
            
    def create_state_updater(self):
        codeobj = self.create_codeobj("state updater",
                                      self.abstract_code,
                                      self.specifiers,
                                      self.language.template_state_update,
                                      )
        self.state_update_codeobj = codeobj
        self.state_updater = CodeRunner(codeobj,
                                        name=self.name+'_state_updater',
                                        when=(self.clock, 'groups'))
        
    def create_thresholder(self, threshold):
        if threshold is None:
            self.thresholder = None
            return
        abstract_code = '_cond = '+threshold # assume threshold is string
        additional_ns = {
            '_spikes': self.spikes,
            '_spikes_space': zeros(self.N, dtype=int),
            '_array_num_spikes': zeros(1, dtype=int),
            }
        codeobj = self.create_codeobj("thresholder",
                                      abstract_code,
                                      self.specifiers,
                                      self.language.template_threshold,
                                      additional_ns,
                                      )
        self.thresholder_codeobj = codeobj
        self.thresholder = Thresholder(self, codeobj,
                                       name=self.name+'_thresholder',
                                       when=(self.clock, 'thresholds'))
        
    def create_resetter(self, reset):
        if reset is None:
            self.resetter = None
            return
        specs = self.specifiers
        specs['_neuron_idx'] = Index(all=False)
        abstract_code = reset # assume reset is string
        additional_ns = {
            '_spikes': self.spikes,
            '_num_spikes': len(self.spikes),
            }
        codeobj = self.create_codeobj("resetter",
                                      abstract_code,
                                      specs,
                                      self.language.template_reset,
                                      additional_ns,
                                      )
        self.resetter_codeobj = codeobj
        self.resetter = Resetter(self, codeobj,
                                 name=self.name+'_resetter',
                                 when=(self.clock, 'resets'))
        
    def get_specifiers(self):
        '''
        Returns a dictionary of `Specifier` objects.
        '''
        # Standard specifiers always present
        s = {'_neuron_idx': Index(all=True),
             'dt': Value(np.float64),
             't': Value(np.float64)}
        # TODO: What about xi?
        for eq in self.equations.equations.itervalues():
            if eq.eq_type in ('diff_equation', 'parameter'):
                s.update({eq.varname: ArrayVariable('_array_'+eq.varname,
                          '_neuron_idx', self.dtypes[eq.varname])})
            elif eq.eq_type == 'static_equation':                
                s.update({eq.varname: Subexpression(str(eq.expr.frozen()))})
            else:
                raise AssertionError('Unknown equation type "%s"' % eq.eq_type)
        
        return s        
    
    abstract_code = property(lambda self: self.method(self.equations))
    specifiers = property(get_specifiers)
    

if __name__=='__main__':
    from pylab import *
    from brian2 import *
    from brian2.codegen.languages import *
    import time

    N = 100
    tau = 10*ms
    eqs = '''
    dV/dt = (2-V)/tau : 1
    '''
    threshold = 'V>1'
    reset = 'V = 0'
    G = NeuronGroup(N, eqs,
                    threshold=threshold,
                    reset=reset,
                    #language=CPPLanguage()
                    language=NumexprPythonLanguage(),
                    )
    #raise Exception
    G.V = rand(N)
    recvals = []
    times = []
    i = []
    t = []
    V = G.V_
    @network_operation
    def recup(self):
        recvals.append(V[0:3].copy())
        times.append(defaultclock.t_)
        for j in G.spikes:
            t.append(defaultclock.t_)
            i.append(j)
    start = time.time()
    run(1*ms)
    print 'Initialise time:', time.time()-start
    start = time.time()
    run(99*ms)
    print 'Runtime:', time.time()-start
    subplot(121)
    plot(times, recvals)
    subplot(122)
    plot(t, i, '.k')
    show()