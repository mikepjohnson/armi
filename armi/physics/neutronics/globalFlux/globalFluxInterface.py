# Copyright 2019 TerraPower, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The Global flux interface provide a base class for all neutronics tools that compute the neutron and/or photon flux."""
import math
from typing import Dict, Optional

import numpy
import scipy.integrate

from armi import interfaces
from armi import runLog
from armi.physics import constants
from armi.physics import executers
from armi.physics import neutronics
from armi.reactor import geometry
from armi.reactor import reactors
from armi.reactor.blocks import Block
from armi.reactor.converters import geometryConverters
from armi.reactor.converters import uniformMesh
from armi.reactor.flags import Flags
from armi.settings.caseSettings import Settings
from armi.utils import units, codeTiming, getMaxBurnSteps

ORDER = interfaces.STACK_ORDER.FLUX

RX_ABS_MICRO_LABELS = ["nGamma", "fission", "nalph", "np", "nd", "nt"]
RX_PARAM_NAMES = ["rateCap", "rateFis", "rateProdN2n", "rateProdFis", "rateAbs"]


class GlobalFluxInterface(interfaces.Interface):
    """
    A general abstract interface for global flux-calculating modules.

    Should be subclassed by more specific implementations.
    """

    name = "GlobalFlux"  # make sure to set this in subclasses
    function = "globalFlux"
    _ENERGY_BALANCE_REL_TOL = 1e-5

    def __init__(self, r, cs):
        interfaces.Interface.__init__(self, r, cs)
        if self.cs["nCycles"] > 1000:
            self.cycleFmt = "04d"  # produce ig0001.inp
        else:
            self.cycleFmt = "03d"  # produce ig001.inp

        if getMaxBurnSteps(self.cs) > 10:
            self.nodeFmt = "03d"  # produce ig001_001.inp
        else:
            self.nodeFmt = "1d"  # produce ig001_1.inp.
        self._bocKeff = None  # for tracking rxSwing
        self._setTightCouplingDefaults()

    def _setTightCouplingDefaults(self):
        """Enable tight coupling defaults for the interface.

        - allows users to set tightCoupling: true in settings without
          having to specify the specific tightCouplingSettings for this interface.
        - this is splt off from self.__init__ for testing
        """
        if self.coupler is None and self.cs["tightCoupling"]:
            self.coupler = interfaces.TightCoupler(
                "keff", 1.0e-4, self.cs["tightCouplingMaxNumIters"]
            )

    @staticmethod
    def getHistoryParams():
        """Return parameters that will be added to assembly versus time history printouts."""
        return ["detailedDpa", "detailedDpaPeak", "detailedDpaPeakRate"]

    def interactBOC(self, cycle=None):
        interfaces.Interface.interactBOC(self, cycle)
        self.r.core.p.rxSwing = 0.0  # zero out rxSwing until EOC.
        self.r.core.p.maxDetailedDpaThisCycle = 0.0  # zero out cumulative params
        self.r.core.p.dpaFullWidthHalfMax = 0.0
        self.r.core.p.elevationOfACLP3Cycles = 0.0
        self.r.core.p.elevationOfACLP7Cycles = 0.0
        for b in self.r.core.getBlocks():
            b.p.detailedDpaThisCycle = 0.0
            b.p.newDPA = 0.0

    def interactEveryNode(self, cycle, node):
        """
        Calculate flux, power, and keff for this cycle and node.

        Flux, power, and keff are generally calculated at every timestep to ensure flux
        is up to date with the reactor state.
        """
        interfaces.Interface.interactEveryNode(self, cycle, node)

        if self.r.p.timeNode == 0:
            self._bocKeff = self.r.core.p.keff  # track boc keff for rxSwing param.

    def interactCoupled(self, iteration):
        """Runs during a tightly-coupled physics iteration to updated the flux and power."""
        interfaces.Interface.interactCoupled(self, iteration)
        if self.r.p.timeNode == 0:
            self._bocKeff = self.r.core.p.keff  # track boc keff for rxSwing param.

    def interactEOC(self, cycle=None):
        interfaces.Interface.interactEOC(self, cycle)
        if self._bocKeff is not None:
            self.r.core.p.rxSwing = (
                (self.r.core.p.keff - self._bocKeff)
                / self._bocKeff
                * units.ABS_REACTIVITY_TO_PCM
            )

    def checkEnergyBalance(self):
        """Check that there is energy balance between the power generated and the specified power.

        .. impl:: Validate the energy generation matches user specifications.
            :id: I_ARMI_FLUX_CHECK_POWER
            :implements: R_ARMI_FLUX_CHECK_POWER

            This method checks that the global power computed from flux
            evaluation matches the global power specified from the user within a
            tolerance; if it does not, a ``ValueError`` is raised. The
            global power from the flux solve is computed by summing the
            block-wise power in the core. This value is then compared to the
            user-specified power and raises an error if relative difference is
            above :math:`10^{-5}`.
        """
        powerGenerated = (
            self.r.core.calcTotalParam(
                "power", calcBasedOnFullObj=False, generationNum=2
            )
            / units.WATTS_PER_MW
        )
        self.r.core.setPowerIfNecessary()
        specifiedPower = (
            self.r.core.p.power / units.WATTS_PER_MW / self.r.core.powerMultiplier
        )

        if not math.isclose(
            powerGenerated, specifiedPower, rel_tol=self._ENERGY_BALANCE_REL_TOL
        ):
            raise ValueError(
                "The power generated in {} is {} MW, but the user specified power is {} MW.\n"
                "This indicates a software bug. Please report to the developers.".format(
                    self.r.core, powerGenerated, specifiedPower
                )
            )

    def getIOFileNames(self, cycle, node, coupledIter=None, additionalLabel=""):
        """
        Return the input and output file names for this run.

        Parameters
        ----------
        cycle : int
            The cycle number
        node : int
            The burn node number (e.g. 0 for BOC, 1 for MOC, etc.)
        coupledIter : int, optional
            Coupled iteration number (for tightly-coupled cases)
        additionalLabel : str, optional
            An optional tag to the file names to differentiate them
            from another case.

        Returns
        -------
        inName : str
            Input file name
        outName : str
            Output file name
        stdName : str
            Standard output file name
        """
        timeId = (
            "{0:" + self.cycleFmt + "}_{1:" + self.nodeFmt + "}"
        )  # build names with proper number of zeros
        if coupledIter is not None:
            timeId += "_{0:03d}".format(coupledIter)

        inName = (
            self.cs.caseTitle
            + timeId.format(cycle, node)
            + "{}.{}.inp".format(additionalLabel, self.name)
        )
        outName = (
            self.cs.caseTitle
            + timeId.format(cycle, node)
            + "{}.{}.out".format(additionalLabel, self.name)
        )
        stdName = outName.strip(".out") + ".stdout"

        return inName, outName, stdName

    def calculateKeff(self, label="keff"):
        """
        Runs neutronics tool and returns keff without applying it to the reactor.

        Used for things like direct-eigenvalue reactivity coefficients and CR worth iterations.
        For anything more complicated than getting keff, clients should
        call ``getExecuter`` to build their case.
        """
        raise NotImplementedError()


class GlobalFluxInterfaceUsingExecuters(GlobalFluxInterface):
    """
    A global flux interface that makes use of the ARMI Executer system to run.

    Using Executers is optional but seems to allow easy interoperability between
    the myriad global flux solvers in the world.

    If a new global flux solver does not fit easily into the Executer pattern, then
    it will be best to just start from the base GlobalFluxInterface rather than
    trying to adjust the Executer pattern to fit.

    Notes
    -----
    This points library users to the Executer object, which is intended to
    provide commonly-used structure useful for many global flux plugins.
    """

    def interactEveryNode(self, cycle, node):
        """
        Calculate flux, power, and keff for this cycle and node.

        Flux, power, and keff are generally calculated at every timestep to ensure flux
        is up to date with the reactor state.
        """
        executer = self.getExecuter(label=self.getLabel(self.cs.caseTitle, cycle, node))
        executer.run()
        GlobalFluxInterface.interactEveryNode(self, cycle, node)

    def interactCoupled(self, iteration):
        """Runs during a tightly-coupled physics iteration to updated the flux and power."""
        executer = self.getExecuter(
            label=self.getLabel(
                self.cs.caseTitle, self.r.p.cycle, self.r.p.timeNode, iteration
            )
        )
        executer.run()
        GlobalFluxInterface.interactCoupled(self, iteration)

    def getTightCouplingValue(self):
        """Return the parameter value.

        .. impl:: Return k-eff or assembly-wise power distribution for coupled interactions.
            :id: I_ARMI_FLUX_COUPLING_VALUE
            :implements: R_ARMI_FLUX_COUPLING_VALUE

            This method either returns the k-eff or assembly-wise power
            distribution. If the :py:class:`coupler
            <armi.interfaces.TightCoupler>` ``parameter`` member is ``"keff"``,
            then this method returns the computed k-eff from the global flux
            evaluation. If the ``parameter`` value is ``"power"``, then it
            returns a list of power distributions in each assembly. The assembly
            power distributions are lists of values representing the block
            powers that are normalized to unity based on the assembly total
            power. If the value is neither ``"keff"`` or ``"power"``, then this
            method returns ``None``.
        """
        if self.coupler.parameter == "keff":
            return self.r.core.p.keff
        if self.coupler.parameter == "power":
            scaledCorePowerDistribution = []
            for a in self.r.core.getChildren():
                scaledPower = []
                assemPower = sum(b.p.power for b in a)
                for b in a:
                    scaledPower.append(b.p.power / assemPower)

                scaledCorePowerDistribution.append(scaledPower)

            return scaledCorePowerDistribution

        return None

    @staticmethod
    def getOptionsCls():
        """
        Get a blank options object.

        Subclass this to allow generic updating of options.
        """
        return GlobalFluxOptions

    @staticmethod
    def getExecuterCls():
        return GlobalFluxExecuter

    def getExecuterOptions(self, label=None):
        """
        Get an executer options object populated from current user settings and reactor.

        If you want to set settings more deliberately (e.g. to specify a cross section
        library rather than use an auto-derived name), use ``getOptionsCls`` and build
        your own.
        """
        opts = self.getOptionsCls()(label)
        opts.fromUserSettings(self.cs)
        opts.fromReactor(self.r)
        return opts

    def getExecuter(self, options=None, label=None):
        """
        Get executer object for performing custom client calcs.

        This allows plugins to update options in a somewhat generic
        way. For example, reactivity coefficients plugin may want to
        request adjoint flux.
        """
        if options and label:
            raise ValueError(
                f"Cannot supply a label (`{label}`) and options at the same time. "
                "Apply label to options object first."
            )
        opts = options or self.getExecuterOptions(label)
        executer = self.getExecuterCls()(options=opts, reactor=self.r)
        return executer

    def calculateKeff(self, label="keff"):
        """
        Run global flux with current user options and just return keff without applying it.

        Used for things like direct-eigenvalue reactivity coefficients and CR worth iterations.
        """
        executer = self.getExecuter(label=label)
        executer.options.applyResultsToReactor = False
        executer.options.calcReactionRatesOnMeshConversion = False
        output = executer.run()
        return output.getKeff()

    @staticmethod
    def getLabel(caseTitle, cycle, node, iteration=None):
        """
        Make a label (input/output file name) for the executer based on cycle, node, iteration.

        Parameters
        ----------
        caseTitle : str, required
            The caseTitle for the ARMI run
        cycle : int, required
            The cycle number
        node : int, required
            The time node index
        iteration : int, optional
            The coupled iteration index
        """
        if iteration is not None:
            return f"{caseTitle}-flux-c{cycle}n{node}i{iteration}"
        else:
            return f"{caseTitle}-flux-c{cycle}n{node}"


class GlobalFluxOptions(executers.ExecutionOptions):
    """Data structure representing common options in Global Flux Solvers.

    .. impl:: Options for neutronics solvers.
        :id: I_ARMI_FLUX_OPTIONS
        :implements: R_ARMI_FLUX_OPTIONS

        This class functions as a data structure for setting and retrieving
        execution options for performing flux evaluations, these options
        involve:

        * What sort of problem is to be solved, i.e. real/adjoint,
          eigenvalue/fixed-source, neutron/gamma, boundary conditions
        * Convergence criteria for iterative algorithms
        * Geometry type and mesh conversion details
        * Specific parameters to be calculated after flux has been evaluated

        These options can be retrieved by directly accessing class members. The
        options are set by specifying a :py:class:`Settings
        <armi.settings.caseSettings.Settings>` object and optionally specifying
        a :py:class:`Reactor <armi.reactor.reactors.Reactor>` object.

    Attributes
    ----------
    adjoint : bool
        True if the ``CONF_NEUTRONICS_TYPE`` setting is set to ``adjoint`` or ``real``.
    calcReactionRatesOnMeshConversion : bool
        This option is used to recalculate reaction rates after a mesh
        conversion and remapping of neutron flux. This can be disabled
        in certain global flux implementations if reaction rates are not
        required, but by default it is enabled.
    eigenvalueProblem : bool
        Whether this is a eigenvalue problem or a fixed source problem
    includeFixedSource : bool
        This can happen in eig if Fredholm Alternative satisfied.
    photons : bool
        Run the photon/gamma uniform mesh converter?
    real : bool
        True if  ``CONF_NEUTRONICS_TYPE`` setting is set to ``real``.
    aclpDoseLimit : float
        Dose limit in dpa used to position the above-core load pad (if one exists)
    boundaries : str
        External Neutronic Boundary Conditions. Reflective does not include axial.
    cs : Settings
        Settings for this run
    detailedAxialExpansion : bool
        Turn on detailed axial expansion? from settings
    dpaPerFluence : float
        A quick and dirty conversion that is used to get dpaPeak by multiplying
        the factor and fastFluencePeak
    energyDepoCalcMethodStep : str
        For gamma transport/normalization
    epsEigenvalue : float
        Convergence criteria for calculating the eigenvalue in the global flux solver
    epsFissionSourceAvg : float
        Convergence criteria for average fission source, from settings
    epsFissionSourcePoint : float
        Convergence criteria for point fission source, from settings
    geomType : geometry.GeomType
        Reactor Core geometry type (HEX, RZ, RZT, etc)
    hasNonUniformAssems: bool
        Has any non-uniform assembly flags, from settings
    isRestart : bool
        Restart global flux case using outputs from last time as a guess
    kernelName : str
        The neutronics / depletion solver for global flux solve.
    loadPadElevation : float
        The elevation of the bottom of the above-core load pad (ACLP) from
        the bottom of the upper grid plate (in cm).
    loadPadLength : float
        The length of the load pad. Used to compute average and peak dose.
    maxOuters : int
        XY and Axial partial current sweep max outer iterations.
    savePhysicsFilesList : bool
        Is this timestamp in the list of savePhysicsFiles in the settings?
    symmetry : str
        Reactor symmetry: full core, third-core, etc
    xsKernel : str
        Lattice Physics Kernel, from settings
    """

    def __init__(self, label: Optional[str] = None):
        executers.ExecutionOptions.__init__(self, label)
        # have defaults
        self.adjoint: bool = False
        self.calcReactionRatesOnMeshConversion: bool = True
        self.eigenvalueProblem: bool = True
        self.includeFixedSource: bool = False
        self.photons: bool = False
        self.real: bool = True

        # no defaults
        self.aclpDoseLimit: Optional[float] = None
        self.boundaries: Optional[str] = None
        self.cs: Optional[Settings] = None
        self.detailedAxialExpansion: Optional[bool] = None
        self.dpaPerFluence: Optional[float] = None
        self.energyDepoCalcMethodStep: Optional[str] = None
        self.epsEigenvalue: Optional[float] = None
        self.epsFissionSourceAvg: Optional[float] = None
        self.epsFissionSourcePoint: Optional[float] = None
        self.geomType: Optional[geometry.GeomType] = None
        self.hasNonUniformAssems: Optional[bool] = None
        self.isRestart: Optional[bool] = None
        self.kernelName: Optional[str] = None
        self.loadPadElevation: Optional[float] = None
        self.loadPadLength: Optional[float] = None
        self.maxOuters: Optional[int] = None
        self.savePhysicsFilesList: Optional[bool] = None
        self.symmetry: Optional[str] = None
        self.xsKernel: Optional[str] = None

    def fromUserSettings(self, cs: Settings):
        """
        Map user input settings from cs to a set of specific global flux options.

        This is not required; these options can alternatively be set programmatically.
        """
        from armi.physics.neutronics.settings import (
            CONF_ACLP_DOSE_LIMIT,
            CONF_BOUNDARIES,
            CONF_DPA_PER_FLUENCE,
            CONF_EIGEN_PROB,
            CONF_LOAD_PAD_ELEVATION,
            CONF_LOAD_PAD_LENGTH,
            CONF_NEUTRONICS_KERNEL,
            CONF_RESTART_NEUTRONICS,
            CONF_XS_KERNEL,
        )
        from armi.settings.fwSettings.globalSettings import (
            CONF_PHYSICS_FILES,
            CONF_NON_UNIFORM_ASSEM_FLAGS,
            CONF_DETAILED_AXIAL_EXPANSION,
        )

        self.kernelName = cs[CONF_NEUTRONICS_KERNEL]
        self.setRunDirFromCaseTitle(cs.caseTitle)
        self.isRestart = cs[CONF_RESTART_NEUTRONICS]
        self.adjoint = neutronics.adjointCalculationRequested(cs)
        self.real = neutronics.realCalculationRequested(cs)
        self.detailedAxialExpansion = cs[CONF_DETAILED_AXIAL_EXPANSION]
        self.hasNonUniformAssems = any(
            [Flags.fromStringIgnoreErrors(f) for f in cs[CONF_NON_UNIFORM_ASSEM_FLAGS]]
        )
        self.eigenvalueProblem = cs[CONF_EIGEN_PROB]

        # dose/dpa specific (should be separate subclass?)
        self.dpaPerFluence = cs[CONF_DPA_PER_FLUENCE]
        self.aclpDoseLimit = cs[CONF_ACLP_DOSE_LIMIT]
        self.loadPadElevation = cs[CONF_LOAD_PAD_ELEVATION]
        self.loadPadLength = cs[CONF_LOAD_PAD_LENGTH]
        self.boundaries = cs[CONF_BOUNDARIES]
        self.xsKernel = cs[CONF_XS_KERNEL]
        self.cs = cs
        self.savePhysicsFilesList = cs[CONF_PHYSICS_FILES]

    def fromReactor(self, reactor: reactors.Reactor):
        self.geomType = reactor.core.geomType
        self.symmetry = reactor.core.symmetry

        cycleNodeStamp = f"{reactor.p.cycle:03d}{reactor.p.timeNode:03d}"
        if self.savePhysicsFilesList:
            self.savePhysicsFiles = cycleNodeStamp in self.savePhysicsFilesList
        else:
            self.savePhysicsFiles = False


class GlobalFluxExecuter(executers.DefaultExecuter):
    """
    A short-lived object that coordinates the prep, execution, and processing of a flux solve.

    There are many forms of global flux solves:

    * Eigenvalue/Fixed source
    * Adjoint/real
    * Diffusion/PN/SN/MC
    * Finite difference/nodal

    There are also many reasons someone might need a flux solve:

    * Update multigroup flux and power on reactor and compute keff
    * Just compute keff in a temporary perturbed state
    * Just compute flux and adjoint flux on a state to

    There may also be some required transformations when a flux solve is done:

    * Add/remove edge assemblies
    * Apply a uniform axial mesh

    There are also I/O performance complexities, including running on fast local paths
    and copying certain user-defined files back to the working directory on error
    or completion. Given all these options and possible needs for information from
    global flux, this class provides a unified interface to everything.

    .. impl:: Ensure the mesh in the reactor model is appropriate for neutronics solver execution.
        :id: I_ARMI_FLUX_GEOM_TRANSFORM
        :implements: R_ARMI_FLUX_GEOM_TRANSFORM

        The primary purpose of this class is perform geometric and mesh
        transformations on the reactor model to ensure a flux evaluation can
        properly perform. This includes:

        * Applying a uniform axial mesh for the 3D flux solve
        * Expanding symmetrical geometries to full-core if necessary
        * Adding/removing edge assemblies if necessary
        * Undoing any transformations that might affect downstream calculations
    """

    def __init__(self, options: GlobalFluxOptions, reactor):
        executers.DefaultExecuter.__init__(self, options, reactor)
        self.options: GlobalFluxOptions
        self.geomConverters: Dict[str, geometryConverters.GeometryConverter] = {}

    @codeTiming.timed
    def _performGeometryTransformations(self, makePlots=False):
        """
        Apply geometry conversions to make reactor work in neutronics.

        There are two conditions where things must happen:

        1. If you are doing finite-difference, you need to add the edge assemblies (fast).
           For this, we just modify the reactor in place

        2. If you are doing detailed axial expansion, you need to average out the axial mesh (slow!)
           For this we need to create a whole copy of the reactor and use that.

        In both cases, we need to undo the modifications between reading the output
        and applying the result to the data model.

        See Also
        --------
        _undoGeometryTransformations
        """
        if any(self.geomConverters):
            raise RuntimeError(
                "The reactor has been transformed, but not restored to the original.\n"
                + "Geometry converter is set to {} \n.".format(self.geomConverters)
                + "This is a programming error and requires further investigation."
            )
        neutronicsReactor = self.r
        converter = self.geomConverters.get("axial")
        if not converter:
            if self.options.detailedAxialExpansion or self.options.hasNonUniformAssems:
                converter = uniformMesh.converterFactory(self.options)
                converter.convert(self.r)
                neutronicsReactor = converter.convReactor

                if makePlots:
                    converter.plotConvertedReactor()

                self.geomConverters["axial"] = converter

        if self.edgeAssembliesAreNeeded():
            converter = self.geomConverters.get(
                "edgeAssems", geometryConverters.EdgeAssemblyChanger()
            )
            converter.addEdgeAssemblies(neutronicsReactor.core)
            self.geomConverters["edgeAssems"] = converter

        self.r = neutronicsReactor

    @codeTiming.timed
    def _undoGeometryTransformations(self):
        """
        Restore original data model state and/or apply results to it.

        Notes
        -----
        These transformations occur in the opposite order than that which they were applied in.
        Otherwise, the uniform mesh guy would try to add info to assem's on the source reactor
        that don't exist.

        See Also
        --------
        _performGeometryTransformations
        """
        geomConverter = self.geomConverters.get("edgeAssems")
        if geomConverter:
            geomConverter.scaleParamsRelatedToSymmetry(
                self.r, paramsToScaleSubset=self.options.paramsToScaleSubset
            )

            # Resets the reactor core model to the correct symmetry and removes
            # stored attributes on the converter to ensure that there is
            # state data that is long-lived on the object in case the garbage
            # collector does not remove it. Additionally, this will reset the
            # global assembly counter.
            geomConverter.removeEdgeAssemblies(self.r.core)

        meshConverter = self.geomConverters.get("axial")

        if meshConverter:
            if self.options.applyResultsToReactor or self.options.hasNonUniformAssems:
                meshConverter.applyStateToOriginal()
            self.r = meshConverter._sourceReactor

            # Resets the stored attributes on the converter to
            # ensure that there is state data that is long-lived on the
            # object in case the garbage collector does not remove it.
            # Additionally, this will reset the global assembly counter.
            meshConverter.reset()

        # clear the converters in case this function gets called twice
        self.geomConverters = {}

    def edgeAssembliesAreNeeded(self) -> bool:
        """
        True if edge assemblies are needed in this calculation.

        We only need them in finite difference cases that are not full core.
        """
        return (
            "FD" in self.options.kernelName
            and self.options.symmetry.domain == geometry.DomainType.THIRD_CORE
            and self.options.symmetry.boundary == geometry.BoundaryType.PERIODIC
            and self.options.geomType == geometry.GeomType.HEX
        )


class GlobalFluxResultMapper(interfaces.OutputReader):
    """
    A short-lived class that maps neutronics output data to a reactor mode.

    Neutronics results can come from a file or a pipe or in memory.
    This is always subclassed for specific neutronics runs but contains
    some generic methods that are universally useful for
    any global flux calculation. These are mostly along the lines of
    information that can be derived from other information, like
    dpa rate coming from dpa deltas and cycle length.
    """

    def getKeff(self):
        raise NotImplementedError()

    def clearFlux(self):
        """Delete flux on all blocks. Needed to prevent stale flux when partially reloading."""
        for b in self.r.core.getBlocks():
            b.p.mgFlux = []
            b.p.adjMgFlux = []
            b.p.mgFluxGamma = []
            b.p.extSrc = []

    def _renormalizeNeutronFluxByBlock(self, renormalizationCorePower):
        """
        Normalize the neutron flux within each block to meet the renormalization power.

        Parameters
        ----------
        renormalizationCorePower: float
            Specified power to renormalize the neutron flux for using the isotopic energy
            generation rates on the cross section libraries (in Watts)

        See Also
        --------
        getTotalEnergyGenerationConstants
        """
        # update the block power param here as well so
        # the ratio/multiplications below are consistent
        currentCorePower = 0.0
        for b in self.r.core.getBlocks():
            # The multi-group flux is volume integrated, so J/cm * n-cm/s gives units of Watts
            b.p.power = numpy.dot(
                b.getTotalEnergyGenerationConstants(), b.getIntegratedMgFlux()
            )
            b.p.flux = sum(b.getMgFlux())
            currentCorePower += b.p.power

        powerRatio = renormalizationCorePower / currentCorePower
        runLog.info(
            "Renormalizing the neutron flux in {:<s} by a factor of {:<8.5e}, "
            "which is derived from the current core power of {:<8.5e} W and "
            "desired power of {:<8.5e} W".format(
                self.r.core, powerRatio, currentCorePower, renormalizationCorePower
            )
        )
        for b in self.r.core.getBlocks():
            b.p.mgFlux *= powerRatio
            b.p.flux *= powerRatio
            b.p.fluxPeak *= powerRatio
            b.p.power *= powerRatio
            b.p.pdens = b.p.power / b.getVolume()

    def _updateDerivedParams(self):
        """Computes some params that are derived directly from flux and power parameters."""
        for maxParamKey in ["percentBu", "pdens"]:
            maxVal = self.r.core.getMaxBlockParam(maxParamKey, Flags.FUEL)
            if maxVal != 0.0:
                self.r.core.p["max" + maxParamKey] = maxVal

        maxFlux = self.r.core.getMaxBlockParam("flux")
        self.r.core.p.maxFlux = maxFlux

        conversion = units.CM2_PER_M2 / units.WATTS_PER_MW
        for a in self.r.core:
            area = a.getArea()
            for b in a:
                b.p.arealPd = b.p.power / area * conversion
            a.p.arealPd = a.calcTotalParam("arealPd")
        self.r.core.p.maxPD = self.r.core.getMaxParam("arealPd")
        self._updateAssemblyLevelParams()

    def getDpaXs(self, b: Block):
        """Determine which cross sections should be used to compute dpa for a block.

        Parameters
        ----------
        b: Block
            The block we want the cross sections for

        Returns
        -------
            list : cross section values
        """
        from armi.physics.neutronics.settings import (
            CONF_DPA_XS_SET,
            CONF_GRID_PLATE_DPA_XS_SET,
        )

        if self.cs[CONF_GRID_PLATE_DPA_XS_SET] and b.hasFlags(Flags.GRID_PLATE):
            dpaXsSetName = self.cs[CONF_GRID_PLATE_DPA_XS_SET]
        else:
            dpaXsSetName = self.cs[CONF_DPA_XS_SET]

        try:
            return constants.DPA_CROSS_SECTIONS[dpaXsSetName]
        except KeyError:
            raise KeyError(
                "DPA cross section set {} does not exist".format(dpaXsSetName)
            )

    def getBurnupPeakingFactor(self, b: Block):
        """
        Get the radial peaking factor to be applied to burnup and DPA for a Block.

        This may be informed by previous runs which used
        detailed pin reconstruction and rotation. In that case,
        it should be set on the cs setting ``burnupPeakingFactor``.

        Otherwise, it just takes the current flux peaking, which
        is typically conservatively high.

        Parameters
        ----------
        b: Block
            The block we want the peaking factor for

        Returns
        -------
        burnupPeakingFactor : float
            The peak/avg factor for burnup and DPA.
        """
        burnupPeakingFactor = self.cs["burnupPeakingFactor"]
        if not burnupPeakingFactor and b.p.fluxPeak:
            burnupPeakingFactor = b.p.fluxPeak / b.p.flux
        elif not burnupPeakingFactor:
            # no peak available. Finite difference model?
            # Use 0.0 for peaking so that there isn't misuse of peaking values that don't actually have peaking applied.
            # Uet self.cs["burnupPeakingFactor"] or b.p.fluxPeak for different behavior
            burnupPeakingFactor = 0.0

        return burnupPeakingFactor

    def updateDpaRate(self, blockList=None):
        """
        Update state parameters that can be known right after the flux is computed.

        See Also
        --------
        updateFluenceAndDpa : uses values computed here to update cumulative dpa
        """
        if blockList is None:
            blockList = self.r.core.getBlocks()

        hasDPA = False
        for b in blockList:
            xs = self.getDpaXs(b)
            hasDPA = True
            flux = b.getMgFlux()  # n/cm^2/s
            dpaPerSecond = computeDpaRate(flux, xs)
            b.p.detailedDpaPeakRate = dpaPerSecond * self.getBurnupPeakingFactor(b)
            b.p.detailedDpaRate = dpaPerSecond

        if not hasDPA:
            return

        peakRate = self.r.core.getMaxBlockParam(
            "detailedDpaPeakRate", typeSpec=Flags.GRID_PLATE, absolute=False
        )
        self.r.core.p.peakGridDpaAt60Years = peakRate * 60.0 * units.SECONDS_PER_YEAR

        # also update maxes at this point (since this runs at every timenode, not just those w/ depletion steps)
        self.updateMaxDpaParams()

    def updateMaxDpaParams(self):
        """
        Update params that track the peak dpa.

        Only consider fuel because CRs, etc. aren't always reset.
        """
        maxDpa = self.r.core.getMaxBlockParam("detailedDpaPeak", Flags.FUEL)
        self.r.core.p.maxdetailedDpaPeak = maxDpa
        self.r.core.p.maxDPA = maxDpa

        # add grid plate max
        maxGridDose = self.r.core.getMaxBlockParam("detailedDpaPeak", Flags.GRID_PLATE)
        self.r.core.p.maxGridDpa = maxGridDose

    def _updateAssemblyLevelParams(self):
        for a in self.r.core.getAssemblies():
            totalAbs = 0.0  # for calculating assembly average k-inf
            totalSrc = 0.0
            for b in a:
                totalAbs += b.p.rateAbs
                totalSrc += b.p.rateProdNet

            a.p.maxPercentBu = a.getMaxParam("percentBu")
            a.p.maxDpaPeak = a.getMaxParam("detailedDpaPeak")
            a.p.timeToLimit = a.getMinParam("timeToLimit", Flags.FUEL)
            a.p.buLimit = a.getMaxParam("buLimit")

            # self.p.kgFis = self.getFissileMass()
            if totalAbs > 0:
                a.p.kInf = totalSrc / totalAbs  # assembly average k-inf.


class DoseResultsMapper(GlobalFluxResultMapper):
    """
    Updates fluence and dpa when time shifts.

    Often called after a depletion step. It is invoked using :py:meth:`apply() <.DoseResultsMapper.apply>`.

    Parameters
    ----------
    depletionSeconds: float, required
        Length of depletion step in units of seconds

    options: GlobalFluxOptions, required
        Object describing options used by the global flux solver. A few attributes from
        this object are used to run the methods in DoseResultsMapper. An example
        attribute is aclpDoseLimit.

    Notes
    -----
    We attempted to make this a set of stateless functions but the requirement of various
    options made it more of a data passing task than we liked. So it's just a lightweight
    and ephemeral results mapper.
    """

    def __init__(self, depletionSeconds, options):
        self.success = False
        self.options = options
        self.cs = self.options.cs
        self.r = None
        self.depletionSeconds = depletionSeconds

    def apply(self, reactor, blockList=None):
        """
        Invokes :py:meth:`updateFluenceAndDpa() <.DoseResultsMapper.updateFluenceAndDpa>`
        for a provided Reactor object.

        Parameters
        ----------
        reactor: Reactor, required
            ARMI Reactor object

        blockList: list, optional
            List of ARMI blocks to be processed by the class. If no blocks are provided, then
            blocks returned by :py:meth:`getBlocks() <.reactors.Core.getBlocks>` are used.

        Returns
        -------
        None
        """
        runLog.extra("Updating fluence and dpa on reactor based on depletion step.")
        self.r = reactor
        self.updateFluenceAndDpa(self.depletionSeconds, blockList=blockList)

    def updateFluenceAndDpa(self, stepTimeInSeconds, blockList=None):
        r"""
        Updates the fast fluence and the DPA of the blocklist.

        The dpa rate from the previous timestep is used to compute the dpa here.

        There are several items of interest computed here, including:
            * detailedDpa: The average DPA of a block
            * detailedDpaPeak: The peak dpa of a block, considering axial and radial peaking
                The peaking is based either on a user-provided peaking factor (computed in a
                pin reconstructed rotation study) or the nodal flux peaking factors
            * dpaPeakFromFluence: fast fluence * fluence conversion factor (old and inaccurate).
                Used to be dpaPeak

        Parameters
        ----------
        stepTimeInSeconds : float
            Time in seconds that the cycle ran at the current mgFlux

        blockList : list, optional
            blocks to be updated. Defaults to all blocks in core

        See Also
        --------
        updateDpaRate : updates the DPA rate used here to compute actual dpa
        """
        blockList = blockList or self.r.core.getBlocks()

        if not blockList[0].p.fluxPeak:
            runLog.warning(
                "no fluxPeak parameter on this model. Peak DPA will be equal to average DPA. "
                "Perhaps you are not running a nodal approximation."
            )

        for b in blockList:
            b.p.residence += stepTimeInSeconds / units.SECONDS_PER_DAY
            b.p.fluence += b.p.flux * stepTimeInSeconds
            b.p.fastFluence += b.p.flux * stepTimeInSeconds * b.p.fastFluxFr
            b.p.fastFluencePeak += b.p.fluxPeak * stepTimeInSeconds * b.p.fastFluxFr

            # update detailed DPA based on dpa rate computed at LAST timestep.
            # new incremental DPA increase for duct distortion interface (and eq)
            b.p.newDPA = b.p.detailedDpaRate * stepTimeInSeconds
            b.p.newDPAPeak = b.p.detailedDpaPeakRate * stepTimeInSeconds

            # use = here instead of += because we need the param system to notice the change for syncronization.
            b.p.detailedDpa = b.p.detailedDpa + b.p.newDPA
            b.p.detailedDpaPeak = b.p.detailedDpaPeak + b.p.newDPAPeak
            b.p.detailedDpaThisCycle = b.p.detailedDpaThisCycle + b.p.newDPA

            # increment point dpas
            # this is specific to hex geometry, but they are general neutronics block parameters
            # if it is a non-hex block, this should be a no-op
            if b.p.pointsCornerDpaRate is not None:
                if b.p.pointsCornerDpa is None:
                    b.p.pointsCornerDpa = numpy.zeros((6,))
                b.p.pointsCornerDpa = (
                    b.p.pointsCornerDpa + b.p.pointsCornerDpaRate * stepTimeInSeconds
                )
            if b.p.pointsEdgeDpaRate is not None:
                if b.p.pointsEdgeDpa is None:
                    b.p.pointsEdgeDpa = numpy.zeros((6,))
                b.p.pointsEdgeDpa = (
                    b.p.pointsEdgeDpa + b.p.pointsEdgeDpaRate * stepTimeInSeconds
                )

            if self.options.dpaPerFluence:
                # do the less rigorous fluence -> DPA conversion if the user gave a factor.
                b.p.dpaPeakFromFluence = (
                    b.p.fastFluencePeak * self.options.dpaPerFluence
                )

            # Set burnup peaking
            # b.p.percentBu/buRatePeak is expected to have been updated elsewhere (depletion)
            # (this should run AFTER burnup has been updated)
            # try to find the peak rate first
            peakRate = None
            if b.p.buRatePeak:
                # best case scenario, we have peak burnup rate
                peakRate = b.p.buRatePeak
            elif b.p.buRate:
                # use whatever peaking factor we can find if just have rate
                peakRate = b.p.buRate * self.getBurnupPeakingFactor(b)

            # If peak rate found, use to calc peak burnup; otherwise scale burnup
            if peakRate:
                # peakRate is in per day
                peakRatePerSecond = peakRate / units.SECONDS_PER_DAY
                b.p.percentBuPeak = (
                    b.p.percentBuPeak + peakRatePerSecond * stepTimeInSeconds
                )
            else:
                # No rate, make bad assumption.... assumes peaking is same at each position through shuffling/irradiation history...
                runLog.warning(
                    "Scaling burnup by current peaking factor... This assumes peaking "
                    "factor was constant through shuffling/irradiation history.",
                    single=True,
                )
                b.p.percentBuPeak = b.p.percentBu * self.getBurnupPeakingFactor(b)

        for a in self.r.core.getAssemblies():
            a.p.daysSinceLastMove += stepTimeInSeconds / units.SECONDS_PER_DAY

        self.updateMaxDpaParams()
        self.updateCycleDoseParams()
        self.updateLoadpadDose()

    def updateCycleDoseParams(self):
        r"""Updates reactor params based on the amount of dose (detailedDpa) accrued this cycle.

        Params updated include:

        * maxDetailedDpaThisCycle
        * dpaFullWidthHalfMax
        * elevationOfACLP3Cycles
        * elevationOfACLP7Cycles

        These parameters are left as zeroes at BOC because no dose has been accumulated yet.
        """
        cycle = self.r.p.cycle
        timeNode = self.r.p.timeNode

        if timeNode <= 0:
            return

        daysIntoCycle = sum(self.r.o.stepLengths[cycle][:timeNode])
        cycleLength = self.r.p.cycleLength

        maxDetailedDpaThisCycle = 0.0
        peakDoseAssem = None
        for a in self.r.core:
            if a.getMaxParam("detailedDpaThisCycle") > maxDetailedDpaThisCycle:
                maxDetailedDpaThisCycle = a.getMaxParam("detailedDpaThisCycle")
                peakDoseAssem = a
        self.r.core.p.maxDetailedDpaThisCycle = maxDetailedDpaThisCycle

        if peakDoseAssem is None:
            return

        doseHalfMaxHeights = peakDoseAssem.getElevationsMatchingParamValue(
            "detailedDpaThisCycle", maxDetailedDpaThisCycle / 2.0
        )
        if len(doseHalfMaxHeights) != 2:
            runLog.warning(
                "Something strange with detailedDpaThisCycle shape in {}, "
                "non-2 number of values matching {}".format(
                    peakDoseAssem, maxDetailedDpaThisCycle / 2.0
                )
            )
        else:
            self.r.core.p.dpaFullWidthHalfMax = (
                doseHalfMaxHeights[1] - doseHalfMaxHeights[0]
            )

        aclpDoseLimit = self.options.aclpDoseLimit
        aclpDoseLimit3 = aclpDoseLimit / 3.0 * (daysIntoCycle / cycleLength)
        aclpLocations3 = peakDoseAssem.getElevationsMatchingParamValue(
            "detailedDpaThisCycle", aclpDoseLimit3
        )
        if len(aclpLocations3) != 2:
            runLog.warning(
                "Something strange with detailedDpaThisCycle shape in {}"
                ", non-2 number of values matching {}".format(
                    peakDoseAssem, aclpDoseLimit / 3.0
                )
            )
        else:
            self.r.core.p.elevationOfACLP3Cycles = aclpLocations3[1]

        aclpDoseLimit7 = aclpDoseLimit / 7.0 * (daysIntoCycle / cycleLength)
        aclpLocations7 = peakDoseAssem.getElevationsMatchingParamValue(
            "detailedDpaThisCycle", aclpDoseLimit7
        )
        if len(aclpLocations7) != 2:
            runLog.warning(
                "Something strange with detailedDpaThisCycle shape in {}, "
                "non-2 number of values matching {}".format(
                    peakDoseAssem, aclpDoseLimit / 7.0
                )
            )
        else:
            self.r.core.p.elevationOfACLP7Cycles = aclpLocations7[1]

    def updateLoadpadDose(self):
        """
        Summarize the dose in DPA of the above-core load pad.

        This sets the following reactor params:

        * loadPadDpaPeak : the peak dpa in any load pad
        * loadPadDpaAvg : the highest average dpa in any load pad

        .. warning:: This only makes sense for cores with load
            pads on their assemblies.

        See Also
        --------
        _calcLoadPadDose : computes the load pad dose

        """
        peakPeak, peakAvg = self._calcLoadPadDose()
        if peakPeak is None:
            return
        self.r.core.p.loadPadDpaAvg = peakAvg[0]
        self.r.core.p.loadPadDpaPeak = peakPeak[0]
        str_ = [
            "Above-core load pad (ACLP) summary. It starts at {0} cm above the "
            "bottom of the grid plate".format(self.options.loadPadElevation)
        ]
        str_.append(
            "The peak ACLP dose is     {0} DPA in {1}".format(peakPeak[0], peakPeak[1])
        )
        str_.append(
            "The max avg. ACLP dose is {0} DPA in {1}".format(peakAvg[0], peakAvg[1])
        )
        runLog.info("\n".join(str_))

    def _calcLoadPadDose(self):
        r"""
        Determines some dose information on the load pads if they exist.

        Expects a few settings to be present in cs
            loadPadElevation : float
                The bottom of the load pad's elevation in cm above the bottom of
                the (upper) grid plate.
            loadPadLength : float
                The axial length of the load pad to average over

        This builds axial splines over the assemblies and then integrates them
        over the load pad.

        The assumptions are that detailedDpa is the average, defined in the center
        and detailedDpaPeak is the peak, also defined in the center of blocks.

        Parameters
        ----------
        core  : armi.reactor.reactors.Core
        cs : armi.settings.caseSettings.Settings

        Returns
        -------
        peakPeak : tuple
            A (peakValue, peakAssem) tuple
        peakAvg : tuple
            a (avgValue, avgAssem) tuple

        See Also
        --------
        writeLoadPadDoseSummary : prints out the dose
        Assembly.getParamValuesAtZ : gets the parameters at any arbitrary z point

        """
        loadPadBottom = self.options.loadPadElevation
        loadPadLength = self.options.loadPadLength
        if not loadPadBottom or not loadPadLength:
            # no load pad dose requested
            return None, None

        peakPeak = (0.0, None)
        peakAvg = (0.0, None)
        loadPadTop = loadPadBottom + loadPadLength

        zrange = numpy.linspace(loadPadBottom, loadPadTop, 100)
        for a in self.r.core.getAssemblies(Flags.FUEL):
            # scan over the load pad to find the peak dpa
            # no caching.
            peakDose = max(
                a.getParamValuesAtZ("detailedDpaPeak", zrange, fillValue="extrapolate")
            )
            # restrict to fuel because control assemblies go nuts in dpa.
            integrand = a.getParamOfZFunction("detailedDpa", fillValue="extrapolate")
            returns = scipy.integrate.quad(integrand, loadPadBottom, loadPadTop)
            avgDose = (
                float(returns[0]) / loadPadLength
            )  # convert to float in case it's an ndarray

            # track max doses
            if peakDose > peakPeak[0]:
                peakPeak = (peakDose, a)
            if avgDose > peakAvg[0]:
                peakAvg = (avgDose, a)

        return peakPeak, peakAvg


def computeDpaRate(mgFlux, dpaXs):
    r"""
    Compute the DPA rate incurred by exposure of a certain flux spectrum.

    Parameters
    ----------
    mgFlux : list
        multigroup neutron flux in #/cm^2/s

    dpaXs : list
        DPA cross section in barns to convolute with flux to determine DPA rate

    Returns
    -------
    dpaPerSecond : float
        The dpa/s in this material due to this flux


    .. impl:: Compute DPA rates.
        :id: I_ARMI_FLUX_DPA
        :implements: R_ARMI_FLUX_DPA

        This method calculates DPA rates using the inputted multigroup flux and DPA cross sections.
        Displacements calculated by displacement XS:

        .. math::

            \text{Displacement rate} &= \phi N_{\text{HT9}} \sigma  \\
            &= (\#/\text{cm}^2/s) \cdot (1/cm^3) \cdot (\text{barn})\\
            &= (\#/\text{cm}^5/s) \cdot  \text{(barn)} * 10^{-24} \text{cm}^2/\text{barn} \\
            &= \#/\text{cm}^3/s


        ::

            DPA rate = displacement density rate / (number of atoms/cc)
                    = dr [#/cm^3/s] / (nHT9)  [1/cm^3]
                    = flux * barn * 1e-24


        .. math::

            \frac{\text{dpa}}{s}  = \frac{\phi N \sigma}{N} = \phi * \sigma

        the Number density of the structural material cancels out. It's in the macroscopic
        XS and in the original number of atoms.

    Raises
    ------
    RuntimeError
       Negative dpa rate.

    """
    displacements = 0.0
    if len(mgFlux) != len(dpaXs):
        runLog.warning(
            "Multigroup flux of length {} is incompatible with dpa cross section of length {};"
            "dpa rate will be set do 0.0".format(len(mgFlux), len(dpaXs)),
            single=True,
        )
        return displacements
    for flux, barns in zip(mgFlux, dpaXs):
        displacements += flux * barns
    dpaPerSecond = displacements * units.CM2_PER_BARN

    if dpaPerSecond < 0:
        runLog.warning(
            "Negative DPA rate calculated at {}".format(dpaPerSecond),
            single=True,
            label="negativeDpaPerSecond",
        )
        # ensure physical meaning of dpaPerSecond, it is likely just slighly negative
        if dpaPerSecond < -1.0e-10:
            raise RuntimeError(
                "Calculated DPA rate is substantially negative at {}".format(
                    dpaPerSecond
                )
            )
        dpaPerSecond = 0.0

    return dpaPerSecond


def calcReactionRates(obj, keff, lib):
    r"""
    Compute 1-group reaction rates for this object (usually a block).

    Parameters
    ----------
    obj : Block
        The object to compute reaction rates on. Notionally this could be upgraded to be
        any kind of ArmiObject but with params defined as they are it currently is only
        implemented for a block.

    keff : float
        The keff of the core. This is required to get the neutron production rate correct
        via the neutron balance statement (since nuSigF has a 1/keff term).

    lib : XSLibrary
        Microscopic cross sections to use in computing the reaction rates.


    .. impl:: Return the reaction rates for a given ArmiObject
        :id: I_ARMI_FLUX_RX_RATES
        :implements: R_ARMI_FLUX_RX_RATES

        This method computes 1-group reaction rates for the inputted
        :py:class:`ArmiObject <armi.reactor.composites.ArmiObject>` These
        reaction rates include:

        * fission
        * nufission
        * n2n
        * absorption

        Scatter could be added as well. This function is quite slow so it is
        skipped for now as it is uncommonly needed.

        Reaction rates are:

        .. math::

            \Sigma \phi = \sum_{\text{nuclides}} \sum_{\text{energy}} \Sigma
            \phi

        The units of :math:`N \sigma \phi` are::

            [#/bn-cm] * [bn] * [#/cm^2/s] = [#/cm^3/s]

        The group-averaged microscopic cross section is:

        .. math::

            \sigma_g = \frac{\int_{E g}^{E_{g+1}} \phi(E)  \sigma(E)
            dE}{\int_{E_g}^{E_{g+1}} \phi(E) dE}
    """
    rate = {}
    for simple in RX_PARAM_NAMES:
        rate[simple] = 0.0

    numberDensities = obj.getNumberDensities()

    for nucName, numberDensity in numberDensities.items():
        if numberDensity == 0.0:
            continue
        nucrate = {}
        for simple in RX_PARAM_NAMES:
            nucrate[simple] = 0.0

        nucMc = lib.getNuclide(nucName, obj.getMicroSuffix())
        micros = nucMc.micros

        # absorption is fission + capture (no n2n here)
        mgFlux = obj.getMgFlux()
        for name in RX_ABS_MICRO_LABELS:
            for g, (groupFlux, xs) in enumerate(zip(mgFlux, micros[name])):
                # dE = flux_e*dE
                dphi = numberDensity * groupFlux
                nucrate["rateAbs"] += dphi * xs

                if name != "fission":
                    nucrate["rateCap"] += dphi * xs
                else:
                    nucrate["rateFis"] += dphi * xs
                    # scale nu by keff.
                    nucrate["rateProdFis"] += (
                        dphi * xs * micros.neutronsPerFission[g] / keff
                    )

        for groupFlux, n2nXs in zip(mgFlux, micros.n2n):
            # this n2n xs is reaction based. Multiply by 2.
            dphi = numberDensity * groupFlux
            nucrate["rateProdN2n"] += 2.0 * dphi * n2nXs

        for simple in RX_PARAM_NAMES:
            if nucrate[simple]:
                rate[simple] += nucrate[simple]

    for paramName, val in rate.items():
        obj.p[paramName] = val  # put in #/cm^3/s

    vFuel = obj.getComponentAreaFrac(Flags.FUEL) if rate["rateFis"] > 0.0 else 1.0
    obj.p.fisDens = rate["rateFis"] / vFuel
    obj.p.fisDensHom = rate["rateFis"]
